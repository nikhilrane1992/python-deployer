
from inspect import isfunction
from deployer.host_container import HostsContainer, HostContainer
from deployer.query import Query
from deployer.utils import isclass
from deployer.pseudo_terminal import DummyPty, Pty
from deployer.loggers import DummyLoggerInterface
from deployer.host import Host
from deployer.node_groups import Group

import datetime
import logging
import traceback
from functools import wraps

"""

Better version of service.py

"""
class ActionException(Exception):
    """
    When an action fails.
    """
    def __init__(self, inner_exception, traceback):
        self.inner_exception = inner_exception
        self.traceback = traceback

class required_property(property):
    """
    Placeholder for properties which are required
    when a service is inherit.
    """
    def __init__(self, description=''):
        self.description = description
        self.name = ''
        self.owner = ''

        def fget(obj):
            raise NotImplementedError('Property %s of %s is not defined: %s' % (self.name, self.owner, self.description))

        property.__init__(self, fget)


class RoleMapping(object):
    """
    A role mapping defines which hosts from a parent Node will used in the childnode,
    and for which roles.

    # Example:
        @map_roles(role='parent_role', role2=['parent_role2', 'parent_role3'])

    # If you don't define any role names, they'll map to the name 'host'.
        @map_roles('parent_role', 'another_parent_role')
    """
    def __init__(self, *host_mapping, **mappings):
        if host_mapping:
            mappings = dict(host=host_mapping, **mappings)

        self._mappings = mappings

    def __call__(self, node_class):
        if not isclass(node_class) or not issubclass(node_class, Node):
            raise TypeError('Role mapping decorator incorrectly applied. '
                            '%r is not a Node class' % node_class)

        # Apply role mapping on a copy of the node class.
        return type(node_class.__name__, (node_class, ), {
                    'Hosts': self,
                    # Keep the module, to make sure that inspect.getsourcelines still works.
                    '__module__': node_class.__module__,
                    })

    def apply(self, parent_node_instance):
        """
        Map roles from the parent to the child node.
        (Apply the filter to the HostsContainer from the parent.)
        """
        return HostsContainer({ role: parent_node_instance.hosts.filter(f)._all for role, f in self._mappings.items() })

map_roles = RoleMapping

class DefaultRoleMapping(RoleMapping):
    """
    Default mapping: take the host container from the parent.
    """
    def apply(self, parent_node_instance):
        return parent_node_instance.hosts


class ChildNodeDescriptor(object):
    """
    Every nested Node class definition in a Node will be wrapped by this descriptor. For instance:

    >> class ParentNode(Node):
    >>     class ChildNode(Node):
    >>         pass
    """
    def __init__(self, attr_name, node_class):
        self.attr_name = attr_name
        self._node_class = node_class

    def __get__(self, parent_instance, owner):
        """
        When the child node is retrieved from an instance of the parent node, an instance of the child node
        will be returned (and the hosts from the parent are mapped to the child.)
        """
        if parent_instance:
            new_name = '%s.%s' % (owner.__name__, self.attr_name)

            # When we have a SimpleNode inside a SimpleNode or a NormalNode
            # inside a NormalNode, and the parent instance is isolated,
            # this one should be isolated as well.
            # Or when we have a NormalNode parent and a SimpleNode.JustOne.
            auto_isolate = [
                    # Parent-child
                    (NodeTypes.SIMPLE, NodeTypes.SIMPLE),
                    (NodeTypes.NORMAL, NodeTypes.NORMAL),
                    (NodeTypes.NORMAL, NodeTypes.SIMPLE_ONE),

                    (NodeTypes.SIMPLE_ARRAY, NodeTypes.SIMPLE),
                    (NodeTypes.SIMPLE_ONE, NodeTypes.SIMPLE),
            ]

            if parent_instance._isolated and (parent_instance._node_type, self._node_class._node_type) in auto_isolate:
                class_ = type(new_name, (self._node_class, ), { '_isolated': True })
            else:
                #class_ = self._node_class
                class_ = type(new_name, (self._node_class, ), { })

            return class_(parent=parent_instance)
        else:
            return self._node_class

class QueryDescriptor(object):
    def __init__(self, node_name, attr_name, query):
        self.node_name = node_name
        self.attr_name = attr_name
        self.query = query

    def __get__(self, instance, owner):
        if instance:
            def run(i):
                """
                Wrapper for the query function which properly handles exceptions.
                """
                try:
                    return self.query._query(i)
                except Exception, e:
                    from deployer.exceptions import QueryException
                    raise QueryException(i._service, self.attr_name, self.query, e)

            # Make sure that a nice name is passed to Action
            run.__name__ = str('query:%s' % self.query.__str__())

            return Action(instance, run, is_property=True)
        else:
            return self.query


class ActionDescriptor(object):
    """
    Every instancemethod in a Service will be wrapped by this descriptor.
    """
    def __init__(self, func):
        self._func = func

    def __get__(self, node_instance, owner):
        if node_instance:
            return Action(node_instance, self._func)
        else:
            # TODO: we should avoid this usage. e.g. in "Config.setup(self)"
            #       this causes Action to lack a service instance, and a
            #       retrieval path...

            return Action(None, self._func)
            #raise Exception("Don't retrieve action from the class object. Use instance.action")
            #return self._func


class PropertyDescriptor(object):
    def __init__(self, attribute):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance:
            return Action(instance, self.attribute.fget, is_property=True)
        else:
            return self.attribute


class Env(object):
    """
    Instead of 'self', we give each Node method an instance of
    this class as the first parameter.
    """
    def __init__(self, node, pty=None, logger=None, is_sandbox=False):
        assert isinstance(node, Node)

        self._node = node
        self._pty = pty or DummyPty()
        self._logger = logger or DummyLoggerInterface()
        self._is_sandbox = is_sandbox

        # When the node is callable (when it has a default action),
        # make sure that this env becomes collable as well.
        if callable(self._node):
            # Per instance overriding
            def call(self, *a, **kw):
                return self.__getattr__('__call__')(*a, **kw)

            self.__class__ = type(self.__class__.__name__, (self.__class__,), { '__call__': call })

        # Lock Env
        self._lock_env = True

    def __repr__(self):
        return 'Env(%s)' % self._node.__class__.__name__

    def __wrap_action(self, action):
        """
        Wrap the action in something that causes it to run in this Env, when it's called.
        """
        @wraps(action._func)
        def func(*a, **kw):
            def run_on_node(isolation):
                with isolation._logger.group(action._func, *a, **kw):
                    try:
                        return action._func(isolation, *a, **kw)
                    except ActionException, e:
                        raise ActionException(e.inner_exception, e.traceback)
                    except Exception, e:
                        raise ActionException(e, traceback.format_exc())

            if isinstance(self, SimpleNode) and not self._isolated and \
                                not getattr(action._func, 'dont_isolate_yet', False):

                isolations = list(self)

                # No hosts in SimpleNode. Nothing to do.
                if len(isolations) == 0:
                    print 'Nothing to do. No hosts in %r' % action
                    return [ ]

                # Exactly one host.
                elif len(isolations) == 1:
                    return [ run_on_node(isolations[0]) ]

                # Multiple hosts, but isolate_one_only flag set.
                elif getattr(action._func, 'isolate_one_only', False):
                    # Ask the end-user which one to use.
                    options = [ (i.hosts.host.slug, i) for i in isolations ]
                    i = Console(self._pty).choice('Choose a host', options, allow_random=True)
                    return run_on_node(i)

                # Multiple hosts. Fork for each isolation.
                else:
                    errors = []

                    # Create a callable for each host.
                    def closure(isolation):
                        def call(pty):
                            # Isolation should be an env, but
                            i2 = Env(isolation._node, pty, isolation._logger, isolation._is_sandbox)

                            # Fork logger
                            logger_fork = self._logger.log_fork('On: %r' % i2._node) # TODO: maybe we shouldn't log fork(). It's an abstraction.

                            try:
                                # Run this action on the new service.
                                result = run_on_node(i2)

                                # Succeed
                                logger_fork.set_succeeded()
                                return result
                            except Exception, e:
                                # TODO: handle exception in thread
                                logger_fork.set_failed(e)
                                errors.append(e)
                        return call

                    # For every isolation, create a callable.
                    callables = [ closure(i) for i in isolations ]
                    logging.info('Forking %r (%i pseudo terminals)' % (action, len(callables)))

                    fork_result = self._pty.run_in_auxiliary_ptys(callables)
                    fork_result.join()

                    if errors:
                        # When an error occcured in one fork, raise this error
                        # again in current thread.
                        raise errors[0]
                        #raise Exception('Fork errors: %s' % str(errors)) # TODO: better exception handling.
                    else:
                        # This returns a list of results.
                        return fork_result.result
            else:
                return action._func(self, *a, **kw)

        if action.is_property:
            # Properties are automatically called upon retrieval
            return func()
        else:
            return func


    def initialize_node(self, node_class):
        """
        Dynamically initialize a node from within another node.
        This will make sure that the node class is initialized with the
        correct logger, sandbox and pty settings.

        - node_class, on object, inheriting from Node
        """
        return self.__wrap_node(node_class())

    def __wrap_node(self, node):
        return Env(node, self._pty, self._logger, self._is_sandbox)

    @property
    def hosts(self):
        return HostsContainer(self._node.hosts._hosts, self._pty, self._logger, self._is_sandbox)

    def __getattr__(self, name):
        """
        Retrieve attributes from the Node class, but in case of actions and
        childnodes, wrap it in this environment.
        """
        attr = getattr(self._node, name)

        if isinstance(attr, Action):
            return self.__wrap_action(attr)

        elif isinstance(attr, Node):
            return self.__wrap_node(attr)

        else:
            return attr

    def __setattr__(self, name, value):
        # Only allow setting of attributes when the _lock_env flag has not yet been set.
        try:
            locked = object.__getattribute__(self, '_lock_env')
        except AttributeError, e:
            locked = False

        if locked:
            raise AttributeError('Not allowed to change attributes of the node environment.')
        else:
            super(Env, self).__setattr__(name, value)

    def __iter__(self):
        for node in self._node:
            yield self.__wrap_node(node)

    def __getitem__(self, item):
        return self.__wrap_node(self._node[item])


class NodeTypes:
    NORMAL = 'NORMAL_NODE'
    SIMPLE = 'SIMPLE_NODE'
    SIMPLE_ARRAY = 'SIMPLE_NODE.ARRAY'
    SIMPLE_ONE = 'SIMPLE_NODE.ONE'

class MappingOptions:
    REQUIRED = 'MAPPING_REQUIRED'
    OPTIONAL = 'MAPPING_OPTIONAL'
    NOT_ALLOWED = 'MAPPING_NOT_ALLOWED'

class NodeNestingRules:
    RULES = {
            # Parent - Child
            (NodeTypes.NORMAL, NodeTypes.NORMAL): MappingOptions.OPTIONAL,
            (NodeTypes.NORMAL, NodeTypes.SIMPLE_ARRAY): MappingOptions.REQUIRED,
            (NodeTypes.NORMAL, NodeTypes.SIMPLE_ONE): MappingOptions.REQUIRED,

            ##(NodeTypes.SIMPLE_ARRAY, NodeTypes.SIMPLE): MappingOptions.NOT_ALLOWED,
            ##(NodeTypes.SIMPLE_ONE, NodeTypes.SIMPLE): MappingOptions.NOT_ALLOWED,
            ##(NodeTypes.SIMPLE, NodeTypes.SIMPLE): MappingOptions.NOT_ALLOWED,
            (NodeTypes.SIMPLE_ARRAY, NodeTypes.SIMPLE): MappingOptions.OPTIONAL,
            (NodeTypes.SIMPLE_ONE, NodeTypes.SIMPLE): MappingOptions.OPTIONAL,
            (NodeTypes.SIMPLE, NodeTypes.SIMPLE): MappingOptions.OPTIONAL,

            (NodeTypes.SIMPLE, NodeTypes.NORMAL): MappingOptions.OPTIONAL, # TODO: do we allow this?
            (NodeTypes.SIMPLE_ARRAY, NodeTypes.NORMAL): MappingOptions.OPTIONAL, # TODO: do we allow this?
            (NodeTypes.SIMPLE_ONE, NodeTypes.NORMAL): MappingOptions.OPTIONAL, # TODO: do we allow this?
    }
    @classmethod
    def check(cls, parent, child):
        return (parent, child) in cls.RULES

    @classmethod
    def check_mapping(cls, parent, child, has_mapping):
        mapping_option = cls.RULES[(parent, child)]

        if has_mapping:
            return mapping_option in (MappingOptions.OPTIONAL, MappingOptions.REQUIRED)
        else:
            return mapping_option in (MappingOptions.OPTIONAL, MappingOptions.NOT_ALLOWED)

def _internal(func):
    """ Mark this function as internal. """
    func.internal = True
    return func

class NodeBase(type):
    """
    Metaclass for Node. This takes mostly care of wrapping Node members
    into the correct descriptor, but it does some metaclass magic.
    """
    # Keep track of the order in which nodes are created, so that we can
    # retain the order of nested sub nodes. This global variable is
    # increased after every definition of a Node class.
    creation_counter = 0

    @classmethod
    def _preprocess_attributes(cls, attrs, base):
        """
        Do double-underscore preprocessing of attributes.
        e.g.
        `server__ssl_is_enabled = True` will override the `ssl_is_enabled`
        value of the server object in attrs.
        """
        new_attrs = { }
        override = { } # { attr_to_override: { k->v } }

        # Split attributes in real attributes and "nested overrides".
        for k, v in attrs.items():
            if '__' in k and not k.startswith('__'): # Allow name mangling.
                # Split at __ (only split at the first __, type(...) below
                # does it recursively.)
                attr_to_override, key = k.split('__', 1)

                if attr_to_override in override:
                    override[attr_to_override][key] = v
                else:
                    override[attr_to_override] = { key : v }
            else:
                new_attrs[k] = v

        # Now apply overrides.
        for attr, overrides in override.items():
            first_override = overrides.keys()[0]

            if attr in new_attrs:
                raise Exception("Don't override %s__%s property in the same scope." %
                                (attr, first_override))
            elif hasattr(base, attr):
                original_node = getattr(base, attr)

                if not issubclass(original_node, Node):
                    import pdb; pdb.set_trace()
                    raise Exception('Node override %s__%s is not applied on a Node class.' %
                                    (attr, first_override))
                else:
                    new_attrs[attr] = type(attr, (original_node,), overrides)
            else:
                raise Exception("Couldn't find %s__%s to override." % (attr, first_override))

        return new_attrs

    def __new__(cls, name, bases, attrs):
        # No multiple inheritance allowed.
        if len(bases) > 1:
            # Not sure whether this is a good idea or not, it might be not that bad...
            raise Exception('No multiple inheritance allowed for Nodes')

        # Preprocess __ in attributes
        attrs = cls._preprocess_attributes(attrs, bases[0])

        # Get node type.
        if '_node_type' in attrs:
            node_type = attrs['_node_type']
        else:
            node_type = bases[0]._node_type

        # Do not allow __init__ to be overriden
        if '__init__' in attrs and not getattr(attrs['__init__'], 'internal', False):
            raise TypeError('A Node should not have its own __init__ function.')

        if name != 'Node':
            # Replace actions/childnodes/properties by descriptors
            for attr_name, attr in attrs.items():
                wrapped_attribute = cls._wrap_attribute(attr_name, attr, name, node_type)
                attrs[attr_name] = wrapped_attribute

                if isfunction(attr):
                    # Create aliases
                    if hasattr(attr, 'action_alias'):
                        for a in attr.action_alias:
                            attrs[a] = wrapped_attribute

                # Make sure that Meta is a class, and if it does not yet
                # inherit from Node.Meta, make sure is does.
                if attr_name == 'Meta':
                    if not isclass(attr):
                        raise Exception('Node.Meta should be a class definition')
                    if not issubclass(attr, Node.Meta):
                        attrs[attr_name] = type('Meta', (attr, Node.Meta), { })

        # Set creation order
        attrs['_node_creation_counter'] = cls.creation_counter
        cls.creation_counter += 1

        # Create class
        return type.__new__(cls, name, bases, attrs)

    @classmethod
    def _wrap_attribute(cls, attr_name, attribute, node_name, node_type):
        """
        Wrap a Node attribute into the correct descriptor class.
        """
        # The Hosts definition (should be a Hosts class ore RoleMapping)
        if attr_name == 'Hosts':
            # Validate node type
            if not isclass(attribute) and not isinstance(attribute, RoleMapping):
                raise Exception('Node.Hosts should be a class definition or a RoleMapping instance.')
            return attribute

        # Wrap functions into an ActionDescriptor
        elif isfunction(attribute) and attr_name not in ('__getitem__', '__iter__'):
            return ActionDescriptor(attribute)

        # Wrap Nodes into a ChildNodeDescriptor
        elif isclass(attribute) and issubclass(attribute, Node):
            # Check the node nesting rules.
            has_mapping = bool(attribute.Hosts)

            if not NodeNestingRules.check(node_type, attribute._node_type):
                raise Exception('Invalid nesting of %s in %s (%r in %r).' % (
                            attribute._node_type, node_type, attribute, node_name))

            if not NodeNestingRules.check_mapping(node_type, attribute._node_type, bool(attribute.Hosts)):
                raise Exception('The Node-attribute %s of type %s does not have a valid role_mapping.' %
                                            (attr_name, attribute._node_type))


            return ChildNodeDescriptor(attr_name, attribute)

        # Properties should be wrapped again in an Action
        # descriptor
        elif isinstance(attribute, property):
            if isinstance(attribute, required_property):
                attribute.name = attr_name
                attribute.owner = node_name
            return PropertyDescriptor(attribute)

        # Query objects are like properties and should also be
        # wrapped into a descriptor
        elif isinstance(attribute, Query):
            return QueryDescriptor(node_name, attr_name, attribute)

        else:
            return attribute

    def __setattr__(self, name, value):
        """
        When dynamically, a new function/property/class is assigned to a
        Node class definition, wrap it into the correct descriptor, before
        assigning it to the actual class.
        Note that `self` is a Node class here, not a Node instance.
        """
        wrapped_attribute = self._wrap_attribute(name, value, self.__name__, self._node_type)
        type.__setattr__(self, name, wrapped_attribute)

    def __instancecheck__(self, instance):
        """
        Override isinstance operator.
        We consider an Env object in instance of this class as well if
        env._node is an instance.
        """
        return type.__instancecheck__(self, instance) or (
                    isinstance(instance, Env) and isinstance(instance._node, self))


class SimpleNodeBase(NodeBase):
    @property
    def Array(self):
        """
        'Arrayify' a SimpleNode. This is an explicit step
        to be taken before nesting SimpleNode into a normal Node.
        """
        if self._node_type != NodeTypes.SIMPLE:
            raise Exception('Second .Array operation is not allowed.')

        class SimpleNodeArray(self):
            _node_type = NodeTypes.SIMPLE_ARRAY

        SimpleNodeArray.__name__ = '%s.Array' % self.__name__
        return SimpleNodeArray

    @property
    def JustOne(self):
        """
        When nesting SimpleNode inside a normal Node,
        say that we expect exactly one host for the mapped
        role, so don't act like an array.
        """
        if self._node_type != NodeTypes.SIMPLE:
            raise Exception('Second .JustOne operation is not allowed.')

        class SimpleNode_One(self):
            _node_type = NodeTypes.SIMPLE_ONE

            @_internal
            def __init__(self, parent):
                Node.__init__(self, parent)
                if len(self.hosts.filter('host')) != 1:
                    raise Exception('Invalid initialisation of SimpleNode.JustOne. %i hosts given.' %
                            len(self.hosts.filter('host')))


        SimpleNode_One.__name__ = '%s.Array' % self.__name__
        return SimpleNode_One

class Node(object):
    __metaclass__ = NodeBase
    __slots__ = ('hosts', 'parent')
    _node_type = NodeTypes.NORMAL
    _isolated = False

    node_group = None
    Hosts = None

    def __repr__(self):
        return '<Node %s>' % self.__class__.__name__

    def __new__(cls, parent=None):
        """
        When this is the root node, of type NORMAL, mark is isolated right away.
        """
        if not parent and cls._node_type == NodeTypes.NORMAL:
            new_cls = type(cls.__name__, (cls,), { '_isolated': True })
            return object.__new__(new_cls, parent)
        else:
            return object.__new__(cls, parent)

    @_internal
    def __init__(self, parent=None):
        self.parent = parent
        if self._node_type in (NodeTypes.SIMPLE_ARRAY, NodeTypes.SIMPLE_ONE) and not parent:
            raise Exception('Cannot initialize a node of type %s without a parent' % self._node_type)

        # Create host container (from hosts definition, or mapping from parent hosts.)
        Hosts = self.Hosts or DefaultRoleMapping()

        if isinstance(Hosts, RoleMapping):
            self.hosts = Hosts.apply(parent) if parent else HostsContainer({ })
        else:
            self.hosts = HostsContainer.from_definition(Hosts)

        # TODO: when this is a SimpleNode and a parent was given, do we have to make sure that the
        #       the 'host' is the same, when a mapping was given? I don't think it's necessary.

    def __getitem__(self, index):
        """
        When this is a not-yet-isolated SimpleNode,
        __getitem__ retrieves the instance for this host.

        This returns a specific isolation. In case of multiple dimensions
        (multiple Node-SimpleNode.Array transitions, a tuple should be provided.)
        """
        if self._isolated:
            raise TypeError('__getitem__ on isolated node is not allowed.')

        if isinstance(index, HostContainer):
            index = (index._host, )

        if not isinstance(index, tuple):
            index = (index, )

        for identifier_type in [
                        IsolationIdentifierType.INT_TUPLES,
                        IsolationIdentifierType.HOST_TUPLES,
                        IsolationIdentifierType.HOSTS_SLUG ]:

            for key, node in iter_isolations(self, identifier_type):
                if key == index:
                    return node
        raise KeyError

    def __iter__(self):
        for key, node in iter_isolations(self):
            yield node

class IsolationIdentifierType:
    INT_TUPLES = 'INT_TUPLES'
    HOST_TUPLES = 'HOST_TUPLES'
    HOSTS_SLUG = 'HOSTS_SLUG'

def iter_isolations(node, identifier_type=IsolationIdentifierType.INT_TUPLES):
    """
    Yield (index, Node) for each isolation of this node.
    """
    assert isinstance(node, Node) and not isinstance(node, Env)

    if node._isolated:
        yield (), node
        return

    def get_simple_node_cell(parent, host):
        """
        For a SimpleNode (or array cell), create a SimpleNode instance which
        matches a single cell, that is one Host for the 'host'-role.
        """
        hosts2 = dict(**node.hosts._hosts)
        hosts2['host'] = host

        class SimpleNodeItem(node.__class__):
            _isolated = True
            Hosts = type('Hosts', (object,), hosts2)

        SimpleNodeItem.__name__ = '%s[%s]' % (node.__class__.__name__, host.slug)
        return SimpleNodeItem(parent=parent)

    def get_identifiers(parent_identifier):
        for i, host in enumerate(node.hosts.filter('host')._all):
            if identifier_type == IsolationIdentifierType.INT_TUPLES:
                identifier = (i,)
            elif identifier_type == IsolationIdentifierType.HOST_TUPLES:
                identifier = (host,)
            elif identifier_type == IsolationIdentifierType.HOSTS_SLUG:
                identifier = (host.slug, )

            yield (parent_identifier + identifier, host)

    # For a normal node, the isolation consists of the parent isolations.
    if node._node_type == NodeTypes.NORMAL:
        if node.parent:
            for index, n in iter_isolations(node.parent, identifier_type):
                yield (index, getattr(n, Inspector(node).get_name()))
        else:
            yield ((), node)

    elif node._node_type == NodeTypes.SIMPLE_ARRAY:
        assert node.parent

        for parent_identifier, parent_node in iter_isolations(node.parent, identifier_type):
            for identifier, host in get_identifiers(parent_identifier):
                yield (identifier, get_simple_node_cell(parent_node, host))

    elif node._node_type == NodeTypes.SIMPLE_ONE:
        TODO

    elif node._node_type == NodeTypes.SIMPLE:
        if node.parent:
            for index, n in iter_isolations(node.parent, identifier_type):
                yield (index, getattr(n, Inspector(node).get_name()))
        else:
            for identifier, host in get_identifiers(()):
                yield (identifier, get_simple_node_cell(None, host))


class SimpleNode(Node):
    """
    A SimpleNode is a Node which has only one role, named 'host'.
    Multiple hosts can be given for this role, but all of them will be isolated,
    during execution. This allows parallel executing of functions on each 'cell'.
    """
    __metaclass__ = SimpleNodeBase
    _node_type = NodeTypes.SIMPLE

    @property
    def host(self):
        if self._isolated:
            return self.hosts.get('host')
        else:
            raise AttributeError


class Action(object):
    """
    Service actions, which are defined as just functions, will be wrapped into
    this Action class. When one such action is called, this class will make
    sure that a correct 'env' object is passed into the function as its first
    argument.
    """
    def __init__(self, node_instance, func, is_property=False):
        self._node_instance = node_instance # XXX: this should be the Env object?
        self._func = func # TODO: wrap _func in something that checks whether the first argument is an Env instance.
        self.is_property = is_property

    def __call__(self, env, *a, **kw):
        """
        Call this action using the unbound method.
        """
        if self._node_instance is None and isinstance(env, Env):
            return env._Env__wrap_action(self)(*a, **kw)
        else:
            raise TypeError('Action is not callable. '
                'Please wrap the Node instance in an Env object first.')

    def __repr__(self):
        # Mostly useful for debugging.
        if self._node_instance:
            return '<Action %s.%s>' % (self._node_instance.__class__.__name__, self._func.__name__)
        else:
            return "<Unbound Action %s>" % self._func.__name__

    @property
    def name(self):
        return self._func.__name__

    @property
    def node(self):
        return self._node_instance

    @property
    def node_group(self):
        return self._node_instance.node_group or Group()


def supress_action_result(action):
    """
    When using a deployment shell, don't print the returned result to stdout.
    For example, when the result is superfluous to be printed, because the
    action itself contains already print statements, while the result
    can be useful for the caller.
    """
    action.supress_result = True
    return action

def dont_isolate_yet(func):
    """
    If the node has not yet been separated in serveral parallel, isolated
    nodes per host. Don't do it yet for this function.
    When anothor action of the same host without this decorator is called,
    the node will be split.

    It's for instance useful for reading input, which is similar for all
    isolated executions, (like asking which Git Checkout has to be taken),
    before forking all the threads.

    Note that this will not guarantee that a node will not be split into
    its isolations, it does only say, that it does not have to. It is was
    already been split before, and this is called from a certain isolation,
    we'll keep it like that.
    """
    func.dont_isolate_yet = True
    return func

def isolate_one_only(func):
    """
    When using role isolation, and several hosts are available, run on only
    one role.  Useful for instance, for a database client. it does not make
    sense to run the interactive client on every host which has database
    access.
    """
    func.isolate_one_only = True
    return func

def alias(name):
    """
    Give this node action an alias. It will also be accessable using that
    name in the deployment shell. This is useful, when you want to have special
    characters which are not allowed in Python function names, like dots, in
    the name of an action.
    """
    def decorator(func):
       if hasattr(func, 'action_alias'):
           func.action_alias.append(name)
       else:
           func.action_alias = [ name ]
       return func
    return decorator




import inspect

class Inspector(object):
    """
    Introspection of a Node object.
    """
    def __init__(self, node):
        if isinstance(node, Env):
            self.env = node
            self.node = node._node
            self.__class__ = _EnvInspector
        elif isinstance(node, Node):
            self.env = None
            self.node = node
        else:
            raise Exception('Expecting a Node object')

    def __repr__(self):
        return 'Inspector(node=%s)' % self.node.__class__.__name__

    def get_isolations(self):
        if self._isolated:
            raise Exception('No isolations...') # TODO: better message
        else:
            raise NotImplementedError # TODO:...

    def _filter(self, include_private, filter):
        childnodes = []
        for name in dir(self.node.__class__):
            if not name.startswith('__'):
                if include_private or not name.startswith('_'):
                    try:
                        attr = getattr(self.node, name)
                        if filter(attr):
                            childnodes.append(attr)
                    except AttributeError: # TODO: this shouldn't happen, but it does... ('host' attribute?)
                        pass
        return childnodes

    def get_childnodes(self, include_private=True):
        # TODO: order by _node_creation_counter
        return self._filter(include_private, lambda i: isinstance(i, Node))

    def has_childnode(self, name):
        try:
            self.get_childnode(name)
            return True
        except AttributeError:
            return False

    def get_childnode(self, name):
        for c in self.get_childnodes():
            if Inspector(c).get_name() == name:
                return c
        raise AttributeError('Childnode not found.')

    def get_actions(self, include_private=True):
        return self._filter(include_private, lambda i: isinstance(i, Action))

    def has_action(self, name):
        try:
            self.get_action(name)
            return True
        except AttributeError:
            return False

    def get_action(self, name):
        for a in self.get_actions():
            if a.name == name:
                return a
        raise AttributeError('Action not found.')

    def supress_result_for_action(self, name):
        return getattr(self.get_actions(name), 'supress_result', False)

    def get_path(self):
        """
        Return a list of (Node, name) tuples, defining the path from the root until here.
        """
        result = []
        n = self.node
        while n:
            result.append( (n, Inspector(n).get_name()) )
            n = n.parent

        return result[::-1]

    def get_group(self):
        """
        Return the group to which this node belongs.
        """
        return self.node.node_group or Group()

    def get_name(self):
        return self.node.__class__.__name__.split('.')[-1]

    def get_full_name(self):
        return self.node.__class__.__name__

    def is_callable(self):
        return hasattr(self.node, '__call__')

class _EnvInspector(Inspector):
    """
    When doing the introspection on an Env object, this acts like a proxy and
    makes sure that the result is Env-compatible.
    """
    def get_childnodes(self, include_private=True):
        nodes = Inspector.get_childnodes(self, include_private)
        return map(self.env._Env__wrap_node, nodes)

    def get_childnode(self, name):
        for c in self.get_childnodes():
            if Inspector(c).get_name() == name:
                return c
        raise AttributeError('Childnode not found.')

    def get_actions(self, include_private=True):
        # TODO: determine first a clean API for what this function should return.
        raise NotImplementedError

    def get_action(self, name):
        raise NotImplementedError

    # --> Walk should be done on node instances, because a nested node class is useless without it role mappings.

    # def walk_over_nodes(self, include_self=True, topdown=True):
    #     for s in self.get_childnodes():
    #         yield self.node_class,
