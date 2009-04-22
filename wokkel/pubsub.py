# -*- test-case-name: wokkel.test.test_pubsub -*-
#
# Copyright (c) 2003-2008 Ralph Meijer
# See LICENSE for details.

"""
XMPP publish-subscribe protocol.

This protocol is specified in
U{XEP-0060<http://www.xmpp.org/extensions/xep-0060.html>}.
"""

from zope.interface import implements

from twisted.internet import defer
from twisted.words.protocols.jabber import jid, error, xmlstream
from twisted.words.xish import domish

from wokkel import disco, data_form, generic, shim
from wokkel.subprotocols import IQHandlerMixin, XMPPHandler
from wokkel.iwokkel import IPubSubClient, IPubSubService

# Iq get and set XPath queries
IQ_GET = '/iq[@type="get"]'
IQ_SET = '/iq[@type="set"]'

# Publish-subscribe namespaces
NS_PUBSUB = 'http://jabber.org/protocol/pubsub'
NS_PUBSUB_EVENT = NS_PUBSUB + '#event'
NS_PUBSUB_ERRORS = NS_PUBSUB + '#errors'
NS_PUBSUB_OWNER = NS_PUBSUB + "#owner"
NS_PUBSUB_NODE_CONFIG = NS_PUBSUB + "#node_config"
NS_PUBSUB_META_DATA = NS_PUBSUB + "#meta-data"
NS_PUBSUB_SUBSCRIBE_OPTIONS = NS_PUBSUB + "#subscribe_options"

# XPath to match pubsub requests
PUBSUB_REQUEST = '/iq[@type="get" or @type="set"]/' + \
                    'pubsub[@xmlns="' + NS_PUBSUB + '" or ' + \
                           '@xmlns="' + NS_PUBSUB_OWNER + '"]'

class SubscriptionPending(Exception):
    """
    Raised when the requested subscription is pending acceptance.
    """



class SubscriptionUnconfigured(Exception):
    """
    Raised when the requested subscription needs to be configured before
    becoming active.
    """



class PubSubError(error.StanzaError):
    """
    Exception with publish-subscribe specific condition.
    """
    def __init__(self, condition, pubsubCondition, feature=None, text=None):
        appCondition = domish.Element((NS_PUBSUB_ERRORS, pubsubCondition))
        if feature:
            appCondition['feature'] = feature
        error.StanzaError.__init__(self, condition,
                                         text=text,
                                         appCondition=appCondition)



class BadRequest(error.StanzaError):
    """
    Bad request stanza error.
    """
    def __init__(self, pubsubCondition=None, text=None):
        if pubsubCondition:
            appCondition = domish.Element((NS_PUBSUB_ERRORS, pubsubCondition))
        else:
            appCondition = None
        error.StanzaError.__init__(self, 'bad-request',
                                         text=text,
                                         appCondition=appCondition)



class Unsupported(PubSubError):
    def __init__(self, feature, text=None):
        PubSubError.__init__(self, 'feature-not-implemented',
                                   'unsupported',
                                   feature,
                                   text)



class Subscription(object):
    """
    A subscription to a node.

    @ivar nodeIdentifier: The identifier of the node subscribed to.
                          The root node is denoted by C{None}.
    @ivar subscriber: The subscribing entity.
    @ivar state: The subscription state. One of C{'subscribed'}, C{'pending'},
                 C{'unconfigured'}.
    @ivar options: Optional list of subscription options.
    @type options: C{dict}.
    """

    def __init__(self, nodeIdentifier, subscriber, state, options=None):
        self.nodeIdentifier = nodeIdentifier
        self.subscriber = subscriber
        self.state = state
        self.options = options or {}



class Item(domish.Element):
    """
    Publish subscribe item.

    This behaves like an object providing L{domish.IElement}.

    Item payload can be added using C{addChild} or C{addRawXml}, or using the
    C{payload} keyword argument to C{__init__}.
    """

    def __init__(self, id=None, payload=None):
        """
        @param id: optional item identifier
        @type id: L{unicode}
        @param payload: optional item payload. Either as a domish element, or
                        as serialized XML.
        @type payload: object providing L{domish.IElement} or L{unicode}.
        """

        domish.Element.__init__(self, (NS_PUBSUB, 'item'))
        if id is not None:
            self['id'] = id
        if payload is not None:
            if isinstance(payload, basestring):
                self.addRawXml(payload)
            else:
                self.addChild(payload)



class PubSubRequest(generic.Stanza):
    """
    A publish-subscribe request.

    The set of instance variables used depends on the type of request. If
    a variable is not applicable or not passed in the request, its value is
    C{None}.

    @ivar verb: The type of publish-subscribe request. See L{_requestVerbMap}.
    @type verb: C{str}.

    @ivar affiliations: Affiliations to be modified.
    @type affiliations: C{set}
    @ivar items: The items to be published, as L{domish.Element}s.
    @type items: C{list}
    @ivar itemIdentifiers: Identifiers of the items to be retrieved or
                           retracted.
    @type itemIdentifiers: C{set}
    @ivar maxItems: Maximum number of items to retrieve.
    @type maxItems: C{int}.
    @ivar nodeIdentifier: Identifier of the node the request is about.
    @type nodeIdentifier: C{unicode}
    @ivar nodeType: The type of node that should be created, or for which the
                    configuration is retrieved. C{'leaf'} or C{'collection'}.
    @type nodeType: C{str}
    @ivar options: Configurations options for nodes, subscriptions and publish
                   requests.
    @type options: L{data_form.Form}
    @ivar subscriber: The subscribing entity.
    @type subscriber: L{JID}
    @ivar subscriptionIdentifier: Identifier for a specific subscription.
    @type subscriptionIdentifier: C{unicode}
    @ivar subscriptions: Subscriptions to be modified, as a set of
                         L{Subscription}.
    @type subscriptions: C{set}
    """

    verb = None

    affiliations = None
    items = None
    itemIdentifiers = None
    maxItems = None
    nodeIdentifier = None
    nodeType = None
    options = None
    subscriber = None
    subscriptionIdentifier = None
    subscriptions = None

    # Map request iq type and subelement name to request verb
    _requestVerbMap = {
        ('set', NS_PUBSUB, 'publish'): 'publish',
        ('set', NS_PUBSUB, 'subscribe'): 'subscribe',
        ('set', NS_PUBSUB, 'unsubscribe'): 'unsubscribe',
        ('get', NS_PUBSUB, 'options'): 'optionsGet',
        ('set', NS_PUBSUB, 'options'): 'optionsSet',
        ('get', NS_PUBSUB, 'subscriptions'): 'subscriptions',
        ('get', NS_PUBSUB, 'affiliations'): 'affiliations',
        ('set', NS_PUBSUB, 'create'): 'create',
        ('get', NS_PUBSUB_OWNER, 'default'): 'default',
        ('get', NS_PUBSUB_OWNER, 'configure'): 'configureGet',
        ('set', NS_PUBSUB_OWNER, 'configure'): 'configureSet',
        ('get', NS_PUBSUB, 'items'): 'items',
        ('set', NS_PUBSUB, 'retract'): 'retract',
        ('set', NS_PUBSUB_OWNER, 'purge'): 'purge',
        ('set', NS_PUBSUB_OWNER, 'delete'): 'delete',
        ('get', NS_PUBSUB_OWNER, 'affiliations'): 'affiliationsGet',
        ('set', NS_PUBSUB_OWNER, 'affiliations'): 'affiliationsSet',
        ('get', NS_PUBSUB_OWNER, 'subscriptions'): 'subscriptionsGet',
        ('set', NS_PUBSUB_OWNER, 'subscriptions'): 'subscriptionsSet',
    }

    # Map request verb to request iq type and subelement name
    _verbRequestMap = dict(((v, k) for k, v in _requestVerbMap.iteritems()))

    # Map request verb to parameter handler names
    _parameters = {
        'publish': ['node', 'items'],
        'subscribe': ['nodeOrEmpty', 'jid'],
        'unsubscribe': ['nodeOrEmpty', 'jid'],
        'optionsGet': ['nodeOrEmpty', 'jid'],
        'optionsSet': ['nodeOrEmpty', 'jid', 'options'],
        'subscriptions': [],
        'affiliations': [],
        'create': ['nodeOrNone'],
        'default': ['default'],
        'configureGet': ['nodeOrEmpty'],
        'configureSet': ['nodeOrEmpty', 'configure'],
        'items': ['node', 'maxItems', 'itemIdentifiers'],
        'retract': ['node', 'itemIdentifiers'],
        'purge': ['node'],
        'delete': ['node'],
        'affiliationsGet': [],
        'affiliationsSet': [],
        'subscriptionsGet': [],
        'subscriptionsSet': [],
    }

    def __init__(self, verb=None):
        self.verb = verb


    @staticmethod
    def _findForm(element, formNamespace):
        """
        Find a Data Form.

        Look for an element that represents a Data Form with the specified
        form namespace as a child element of the given element.
        """
        if not element:
            return None

        form = None
        for child in element.elements():
            try:
                form = data_form.Form.fromElement(child)
            except data_form.Error:
                continue

            if form.formNamespace != NS_PUBSUB_NODE_CONFIG:
                continue

        return form


    def _parse_node(self, verbElement):
        """
        Parse the required node identifier out of the verbElement.
        """
        try:
            self.nodeIdentifier = verbElement["node"]
        except KeyError:
            raise BadRequest('nodeid-required')


    def _render_node(self, verbElement):
        """
        Render the required node identifier on the verbElement.
        """
        if not self.nodeIdentifier:
            raise Exception("Node identifier is required")

        verbElement['node'] = self.nodeIdentifier


    def _parse_nodeOrEmpty(self, verbElement):
        """
        Parse the node identifier out of the verbElement. May be empty.
        """
        self.nodeIdentifier = verbElement.getAttribute("node", '')


    def _render_nodeOrEmpty(self, verbElement):
        """
        Render the node identifier on the verbElement. May be empty.
        """
        if self.nodeIdentifier:
            verbElement['node'] = self.nodeIdentifier


    def _parse_nodeOrNone(self, verbElement):
        """
        Parse the optional node identifier out of the verbElement.
        """
        self.nodeIdentifier = verbElement.getAttribute("node")


    def _render_nodeOrNone(self, verbElement):
        """
        Render the optional node identifier on the verbElement.
        """
        if self.nodeIdentifier:
            verbElement['node'] = self.nodeIdentifier


    def _parse_items(self, verbElement):
        """
        Parse items out of the verbElement for publish requests.
        """
        self.items = []
        for element in verbElement.elements():
            if element.uri == NS_PUBSUB and element.name == 'item':
                self.items.append(element)


    def _render_items(self, verbElement):
        """
        Render items into the verbElement for publish requests.
        """
        if self.items:
            for item in self.items:
                verbElement.addChild(item)


    def _parse_jid(self, verbElement):
        """
        Parse subscriber out of the verbElement for un-/subscribe requests.
        """
        try:
            self.subscriber = jid.internJID(verbElement["jid"])
        except KeyError:
            raise BadRequest('jid-required')


    def _render_jid(self, verbElement):
        """
        Render subscriber into the verbElement for un-/subscribe requests.
        """
        verbElement['jid'] = self.subscriber.full()


    def _parse_default(self, verbElement):
        """
        Parse node type out of a request for the default node configuration.
        """
        form = PubSubRequest._findForm(verbElement, NS_PUBSUB_NODE_CONFIG)
        if form and form.formType == 'submit':
            values = form.getValues()
            self.nodeType = values.get('pubsub#node_type', 'leaf')
        else:
            self.nodeType = 'leaf'


    def _parse_configure(self, verbElement):
        """
        Parse options out of a request for setting the node configuration.
        """
        form = PubSubRequest._findForm(verbElement, NS_PUBSUB_NODE_CONFIG)
        if form:
            if form.formType == 'submit':
                self.options = form.getValues()
            elif form.formType == 'cancel':
                self.options = {}
            else:
                raise BadRequest(text="Unexpected form type %r" % form.formType)
        else:
            raise BadRequest(text="Missing configuration form")



    def _parse_itemIdentifiers(self, verbElement):
        """
        Parse item identifiers out of items and retract requests.
        """
        self.itemIdentifiers = []
        for element in verbElement.elements():
            if element.uri == NS_PUBSUB and element.name == 'item':
                try:
                    self.itemIdentifiers.append(element["id"])
                except KeyError:
                    raise BadRequest()


    def _render_itemIdentifiers(self, verbElement):
        """
        Render item identifiers into items and retract requests.
        """
        if self.itemIdentifiers:
            for itemIdentifier in self.itemIdentifiers:
                item = verbElement.addElement('item')
                item['id'] = itemIdentifier


    def _parse_maxItems(self, verbElement):
        """
        Parse maximum items out of an items request.
        """
        value = verbElement.getAttribute('max_items')

        if value:
            try:
                self.maxItems = int(value)
            except ValueError:
                raise BadRequest(text="Field max_items requires a positive " +
                                      "integer value")


    def _render_maxItems(self, verbElement):
        """
        Parse maximum items into an items request.
        """
        if self.maxItems:
            verbElement['max_items'] = unicode(self.maxItems)


    def _parse_options(self, verbElement):
        form = PubSubRequest._findForm(verbElement, NS_PUBSUB_SUBSCRIBE_OPTIONS)
        if form:
            if form.formType == 'submit':
                self.options = form.getValues()
            elif form.formType == 'cancel':
                self.options = {}
            else:
                raise BadRequest(text="Unexpected form type %r" % form.formType)
        else:
            raise BadRequest(text="Missing options form")

    def parseElement(self, element):
        """
        Parse the publish-subscribe verb and parameters out of a request.
        """
        generic.Stanza.parseElement(self, element)

        for child in element.pubsub.elements():
            key = (self.stanzaType, child.uri, child.name)
            try:
                verb = self._requestVerbMap[key]
            except KeyError:
                continue
            else:
                self.verb = verb
                break

        if not self.verb:
            raise NotImplementedError()

        for parameter in self._parameters[verb]:
            getattr(self, '_parse_%s' % parameter)(child)


    def send(self, xs):
        """
        Send this request to its recipient.

        This renders all of the relevant parameters for this specific
        requests into an L{xmlstream.IQ}, and invoke its C{send} method.
        This returns a deferred that fires upon reception of a response. See
        L{xmlstream.IQ} for details.

        @param xs: The XML stream to send the request on.
        @type xs: L{xmlstream.XmlStream}
        @rtype: L{defer.Deferred}.
        """

        try:
            (self.stanzaType,
             childURI,
             childName) = self._verbRequestMap[self.verb]
        except KeyError:
            raise NotImplementedError()

        iq = xmlstream.IQ(xs, self.stanzaType)
        iq.addElement((childURI, 'pubsub'))
        verbElement = iq.pubsub.addElement(childName)

        if self.sender:
            iq['from'] = self.sender.full()
        if self.recipient:
            iq['to'] = self.recipient.full()

        for parameter in self._parameters[self.verb]:
            getattr(self, '_render_%s' % parameter)(verbElement)

        return iq.send()



class PubSubEvent(object):
    """
    A publish subscribe event.

    @param sender: The entity from which the notification was received.
    @type sender: L{jid.JID}
    @param recipient: The entity to which the notification was sent.
    @type recipient: L{wokkel.pubsub.ItemsEvent}
    @param nodeIdentifier: Identifier of the node the event pertains to.
    @type nodeIdentifier: C{unicode}
    @param headers: SHIM headers, see L{wokkel.shim.extractHeaders}.
    @type headers: L{dict}
    """

    def __init__(self, sender, recipient, nodeIdentifier, headers):
        self.sender = sender
        self.recipient = recipient
        self.nodeIdentifier = nodeIdentifier
        self.headers = headers



class ItemsEvent(PubSubEvent):
    """
    A publish-subscribe event that signifies new, updated and retracted items.

    @param items: List of received items as domish elements.
    @type items: C{list} of L{domish.Element}
    """

    def __init__(self, sender, recipient, nodeIdentifier, items, headers):
        PubSubEvent.__init__(self, sender, recipient, nodeIdentifier, headers)
        self.items = items



class DeleteEvent(PubSubEvent):
    """
    A publish-subscribe event that signifies the deletion of a node.
    """

    redirectURI = None



class PurgeEvent(PubSubEvent):
    """
    A publish-subscribe event that signifies the purging of a node.
    """



class PubSubClient(XMPPHandler):
    """
    Publish subscribe client protocol.
    """

    implements(IPubSubClient)

    def connectionInitialized(self):
        self.xmlstream.addObserver('/message/event[@xmlns="%s"]' %
                                   NS_PUBSUB_EVENT, self._onEvent)


    def _onEvent(self, message):
        try:
            sender = jid.JID(message["from"])
            recipient = jid.JID(message["to"])
        except KeyError:
            return

        actionElement = None
        for element in message.event.elements():
            if element.uri == NS_PUBSUB_EVENT:
                actionElement = element

        if not actionElement:
            return

        eventHandler = getattr(self, "_onEvent_%s" % actionElement.name, None)

        if eventHandler:
            headers = shim.extractHeaders(message)
            eventHandler(sender, recipient, actionElement, headers)
            message.handled = True


    def _onEvent_items(self, sender, recipient, action, headers):
        nodeIdentifier = action["node"]

        items = [element for element in action.elements()
                         if element.name in ('item', 'retract')]

        event = ItemsEvent(sender, recipient, nodeIdentifier, items, headers)
        self.itemsReceived(event)


    def _onEvent_delete(self, sender, recipient, action, headers):
        nodeIdentifier = action["node"]
        event = DeleteEvent(sender, recipient, nodeIdentifier, headers)
        if action.redirect:
            event.redirectURI = action.redirect.getAttribute('uri')
        self.deleteReceived(event)


    def _onEvent_purge(self, sender, recipient, action, headers):
        nodeIdentifier = action["node"]
        event = PurgeEvent(sender, recipient, nodeIdentifier, headers)
        self.purgeReceived(event)


    def itemsReceived(self, event):
        pass


    def deleteReceived(self, event):
        pass


    def purgeReceived(self, event):
        pass


    def createNode(self, service, nodeIdentifier=None, sender=None):
        """
        Create a publish subscribe node.

        @param service: The publish subscribe service to create the node at.
        @type service: L{JID}
        @param nodeIdentifier: Optional suggestion for the id of the node.
        @type nodeIdentifier: C{unicode}
        """
        request = PubSubRequest('create')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        request.sender = sender

        def cb(iq):
            try:
                new_node = iq.pubsub.create["node"]
            except AttributeError:
                # the suggested node identifier was accepted
                new_node = nodeIdentifier
            return new_node

        d = request.send(self.xmlstream)
        d.addCallback(cb)
        return d


    def deleteNode(self, service, nodeIdentifier, sender=None):
        """
        Delete a publish subscribe node.

        @param service: The publish subscribe service to delete the node from.
        @type service: L{JID}
        @param nodeIdentifier: The identifier of the node.
        @type nodeIdentifier: C{unicode}
        """
        request = PubSubRequest('delete')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        request.sender = sender
        return request.send(self.xmlstream)


    def subscribe(self, service, nodeIdentifier, subscriber, sender=None):
        """
        Subscribe to a publish subscribe node.

        @param service: The publish subscribe service that keeps the node.
        @type service: L{JID}
        @param nodeIdentifier: The identifier of the node.
        @type nodeIdentifier: C{unicode}
        @param subscriber: The entity to subscribe to the node. This entity
                           will get notifications of new published items.
        @type subscriber: L{JID}
        """
        request = PubSubRequest('subscribe')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        request.subscriber = subscriber
        request.sender = sender

        def cb(iq):
            subscription = iq.pubsub.subscription["subscription"]

            if subscription == 'pending':
                raise SubscriptionPending
            elif subscription == 'unconfigured':
                raise SubscriptionUnconfigured
            else:
                # we assume subscription == 'subscribed'
                # any other value would be invalid, but that should have
                # yielded a stanza error.
                return None

        d = request.send(self.xmlstream)
        d.addCallback(cb)
        return d


    def unsubscribe(self, service, nodeIdentifier, subscriber, sender=None):
        """
        Unsubscribe from a publish subscribe node.

        @param service: The publish subscribe service that keeps the node.
        @type service: L{JID}
        @param nodeIdentifier: The identifier of the node.
        @type nodeIdentifier: C{unicode}
        @param subscriber: The entity to unsubscribe from the node.
        @type subscriber: L{JID}
        """
        request = PubSubRequest('unsubscribe')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        request.subscriber = subscriber
        request.sender = sender
        return request.send(self.xmlstream)


    def publish(self, service, nodeIdentifier, items=None, sender=None):
        """
        Publish to a publish subscribe node.

        @param service: The publish subscribe service that keeps the node.
        @type service: L{JID}
        @param nodeIdentifier: The identifier of the node.
        @type nodeIdentifier: C{unicode}
        @param items: Optional list of L{Item}s to publish.
        @type items: C{list}
        """
        request = PubSubRequest('publish')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        request.items = items
        request.sender = sender
        return request.send(self.xmlstream)


    def items(self, service, nodeIdentifier, maxItems=None, sender=None):
        """
        Retrieve previously published items from a publish subscribe node.

        @param service: The publish subscribe service that keeps the node.
        @type service: L{JID}
        @param nodeIdentifier: The identifier of the node.
        @type nodeIdentifier: C{unicode}
        @param maxItems: Optional limit on the number of retrieved items.
        @type maxItems: C{int}
        """
        request = PubSubRequest('items')
        request.recipient = service
        request.nodeIdentifier = nodeIdentifier
        if maxItems:
            request.maxItems = str(int(maxItems))
        request.sender = sender

        def cb(iq):
            items = []
            for element in iq.pubsub.items.elements():
                if element.uri == NS_PUBSUB and element.name == 'item':
                    items.append(element)
            return items

        d = request.send(self.xmlstream)
        d.addCallback(cb)
        return d



class PubSubService(XMPPHandler, IQHandlerMixin):
    """
    Protocol implementation for a XMPP Publish Subscribe Service.

    The word Service here is used as taken from the Publish Subscribe
    specification. It is the party responsible for keeping nodes and their
    subscriptions, and sending out notifications.

    Methods from the L{IPubSubService} interface that are called as
    a result of an XMPP request may raise exceptions. Alternatively the
    deferred returned by these methods may have their errback called. These are
    handled as follows:

     - If the exception is an instance of L{error.StanzaError}, an error
       response iq is returned.
     - Any other exception is reported using L{log.msg}. An error response
       with the condition C{internal-server-error} is returned.

    The default implementation of said methods raises an L{Unsupported}
    exception and are meant to be overridden.

    @ivar discoIdentity: Service discovery identity as a dictionary with
                         keys C{'category'}, C{'type'} and C{'name'}.
    @ivar pubSubFeatures: List of supported publish-subscribe features for
                          service discovery, as C{str}.
    @type pubSubFeatures: C{list} or C{None}
    """

    implements(IPubSubService)

    iqHandlers = {
            '/*': '_onPubSubRequest',
            }


    def __init__(self):
        self.discoIdentity = {'category': 'pubsub',
                              'type': 'generic',
                              'name': 'Generic Publish-Subscribe Service'}

        self.pubSubFeatures = []


    def connectionMade(self):
        self.xmlstream.addObserver(PUBSUB_REQUEST, self.handleRequest)


    def getDiscoInfo(self, requestor, target, nodeIdentifier):
        info = []

        if not nodeIdentifier:
            category, idType, name = self.discoIdentity
            info.append(disco.DiscoIdentity(category, idType, name))

            info.append(disco.DiscoFeature(disco.NS_DISCO_ITEMS))
            info.extend([disco.DiscoFeature("%s#%s" % (NS_PUBSUB, feature))
                         for feature in self.pubSubFeatures])

        def toInfo(nodeInfo):
            if not nodeInfo:
                return

            (nodeType, metaData) = nodeInfo['type'], nodeInfo['meta-data']
            info.append(disco.DiscoIdentity('pubsub', nodeType))
            if metaData:
                form = data_form.Form(formType="result",
                                      formNamespace=NS_PUBSUB_META_DATA)
                form.addField(
                        data_form.Field(
                            var='pubsub#node_type',
                            value=nodeType,
                            label='The type of node (collection or leaf)'
                        )
                )

                for metaDatum in metaData:
                    form.addField(data_form.Field.fromDict(metaDatum))

                info.append(form)

        d = self.getNodeInfo(requestor, target, nodeIdentifier or '')
        d.addCallback(toInfo)
        d.addBoth(lambda result: info)
        return d


    def getDiscoItems(self, requestor, target, nodeIdentifier):
        if nodeIdentifier or self.hideNodes:
            return defer.succeed([])

        d = self.getNodes(requestor, target)
        d.addCallback(lambda nodes: [disco.DiscoItem(target, node)
                                     for node in nodes])
        return d


    def _onPubSubRequest(self, iq):
        request = PubSubRequest.fromElement(iq)
        handler = getattr(self, '_on_%s' % request.verb)
        return handler(request)


    def _on_publish(self, request):
        return self.publish(request.sender, request.recipient,
                            request.nodeIdentifier, request.items)


    def _on_subscribe(self, request):

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, "pubsub"))
            subscription = response.addElement("subscription")
            if result.nodeIdentifier:
                subscription["node"] = result.nodeIdentifier
            subscription["jid"] = result.subscriber.full()
            subscription["subscription"] = result.state
            return response

        d = self.subscribe(request.sender, request.recipient,
                           request.nodeIdentifier, request.subscriber)
        d.addCallback(toResponse)
        return d


    def _on_unsubscribe(self, request):
        return self.unsubscribe(request.sender, request.recipient,
                                request.nodeIdentifier, request.subscriber)


    def _on_optionsGet(self, request):
        raise Unsupported('subscription-options')


    def _on_optionsSet(self, request):
        raise Unsupported('subscription-options')


    def _on_subscriptions(self, request):

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            subscriptions = response.addElement('subscriptions')
            for subscription in result:
                item = subscriptions.addElement('subscription')
                item['node'] = subscription.nodeIdentifier
                item['jid'] = subscription.subscriber.full()
                item['subscription'] = subscription.state
            return response

        d = self.subscriptions(request.sender, request.recipient)
        d.addCallback(toResponse)
        return d


    def _on_affiliations(self, request):

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            affiliations = response.addElement('affiliations')

            for nodeIdentifier, affiliation in result:
                item = affiliations.addElement('affiliation')
                item['node'] = nodeIdentifier
                item['affiliation'] = affiliation

            return response

        d = self.affiliations(request.sender, request.recipient)
        d.addCallback(toResponse)
        return d


    def _on_create(self, request):

        def toResponse(result):
            if not request.nodeIdentifier or request.nodeIdentifier != result:
                response = domish.Element((NS_PUBSUB, 'pubsub'))
                create = response.addElement('create')
                create['node'] = result
                return response
            else:
                return None

        d = self.create(request.sender, request.recipient,
                        request.nodeIdentifier)
        d.addCallback(toResponse)
        return d


    def _makeFields(self, options, values):
        fields = []
        for name, value in values.iteritems():
            if name not in options:
                continue

            option = {'var': name}
            option.update(options[name])
            if isinstance(value, list):
                option['values'] = value
            else:
                option['value'] = value
            fields.append(data_form.Field.fromDict(option))
        return fields


    def _formFromConfiguration(self, values):
        options = self.getConfigurationOptions()
        fields = self._makeFields(options, values)
        form = data_form.Form(formType="form",
                              formNamespace=NS_PUBSUB_NODE_CONFIG,
                              fields=fields)

        return form


    def _checkConfiguration(self, values):
        options = self.getConfigurationOptions()
        processedValues = {}

        for key, value in values.iteritems():
            if key not in options:
                continue

            option = {'var': key}
            option.update(options[key])
            field = data_form.Field.fromDict(option)
            if isinstance(value, list):
                field.values = value
            else:
                field.value = value
            field.typeCheck()

            if isinstance(value, list):
                processedValues[key] = field.values
            else:
                processedValues[key] = field.value

        return processedValues


    def _on_default(self, request):

        def toResponse(options):
            response = domish.Element((NS_PUBSUB_OWNER, "pubsub"))
            default = response.addElement("default")
            default.addChild(self._formFromConfiguration(options).toElement())
            return response

        if request.nodeType not in ('leaf', 'collection'):
            return defer.fail(error.StanzaError('not-acceptable'))

        d = self.getDefaultConfiguration(request.sender, request.recipient,
                                         request.nodeType)
        d.addCallback(toResponse)
        return d


    def _on_configureGet(self, request):
        def toResponse(options):
            response = domish.Element((NS_PUBSUB_OWNER, "pubsub"))
            configure = response.addElement("configure")
            form = self._formFromConfiguration(options)
            configure.addChild(form.toElement())

            if request.nodeIdentifier:
                configure["node"] = request.nodeIdentifier

            return response

        d = self.getConfiguration(request.sender, request.recipient,
                                  request.nodeIdentifier)
        d.addCallback(toResponse)
        return d


    def _on_configureSet(self, request):
        if request.options:
            request.options = self._checkConfiguration(request.options)
            return self.setConfiguration(request.sender, request.recipient,
                                         request.nodeIdentifier,
                                         request.options)
        else:
            return None



    def _on_items(self, request):

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            items = response.addElement('items')
            items["node"] = request.nodeIdentifier

            for item in result:
                items.addChild(item)

            return response

        d = self.items(request.sender, request.recipient,
                       request.nodeIdentifier, request.maxItems,
                       request.itemIdentifiers)
        d.addCallback(toResponse)
        return d


    def _on_retract(self, request):
        return self.retract(request.sender, request.recipient,
                            request.nodeIdentifier, request.itemIdentifiers)


    def _on_purge(self, request):
        return self.purge(request.sender, request.recipient,
                          request.nodeIdentifier)


    def _on_delete(self, request):
        return self.delete(request.sender, request.recipient,
                           request.nodeIdentifier)


    def _on_affiliationsGet(self, iq):
        raise Unsupported('modify-affiliations')


    def _on_affiliationsSet(self, iq):
        raise Unsupported('modify-affiliations')


    def _on_subscriptionsGet(self, iq):
        raise Unsupported('manage-subscriptions')


    def _on_subscriptionsSet(self, iq):
        raise Unsupported('manage-subscriptions')

    # public methods

    def _createNotification(self, eventType, service, nodeIdentifier,
                                  subscriber, subscriptions=None):
        headers = []

        if subscriptions:
            for subscription in subscriptions:
                if nodeIdentifier != subscription.nodeIdentifier:
                    headers.append(('Collection', subscription.nodeIdentifier))

        message = domish.Element((None, "message"))
        message["from"] = service.full()
        message["to"] = subscriber.full()
        event = message.addElement((NS_PUBSUB_EVENT, "event"))

        element = event.addElement(eventType)
        element["node"] = nodeIdentifier

        if headers:
            message.addChild(shim.Headers(headers))

        return message

    def notifyPublish(self, service, nodeIdentifier, notifications):
        for subscriber, subscriptions, items in notifications:
            message = self._createNotification('items', service,
                                               nodeIdentifier, subscriber,
                                               subscriptions)
            message.event.items.children = items
            self.send(message)


    def notifyDelete(self, service, nodeIdentifier, subscribers,
                           redirectURI=None):
        for subscriber in subscribers:
            message = self._createNotification('delete', service,
                                               nodeIdentifier,
                                               subscriber)
            if redirectURI:
                redirect = message.event.delete.addElement('redirect')
                redirect['uri'] = redirectURI
            self.send(message)


    def getNodeInfo(self, requestor, service, nodeIdentifier):
        return None


    def getNodes(self, requestor, service):
        return []


    def publish(self, requestor, service, nodeIdentifier, items):
        raise Unsupported('publish')


    def subscribe(self, requestor, service, nodeIdentifier, subscriber):
        raise Unsupported('subscribe')


    def unsubscribe(self, requestor, service, nodeIdentifier, subscriber):
        raise Unsupported('subscribe')


    def subscriptions(self, requestor, service):
        raise Unsupported('retrieve-subscriptions')


    def affiliations(self, requestor, service):
        raise Unsupported('retrieve-affiliations')


    def create(self, requestor, service, nodeIdentifier):
        raise Unsupported('create-nodes')


    def getConfigurationOptions(self):
        return {}


    def getDefaultConfiguration(self, requestor, service, nodeType):
        raise Unsupported('retrieve-default')


    def getConfiguration(self, requestor, service, nodeIdentifier):
        raise Unsupported('config-node')


    def setConfiguration(self, requestor, service, nodeIdentifier, options):
        raise Unsupported('config-node')


    def items(self, requestor, service, nodeIdentifier, maxItems,
                    itemIdentifiers):
        raise Unsupported('retrieve-items')


    def retract(self, requestor, service, nodeIdentifier, itemIdentifiers):
        raise Unsupported('retract-items')


    def purge(self, requestor, service, nodeIdentifier):
        raise Unsupported('purge-nodes')


    def delete(self, requestor, service, nodeIdentifier):
        raise Unsupported('delete-nodes')
