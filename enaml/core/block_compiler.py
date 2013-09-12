#------------------------------------------------------------------------------
# Copyright (c) 2013, Nucleic Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#------------------------------------------------------------------------------
from atom.api import Atom, Constant, List, Typed

from .code_generator import CodeGenerator
from .compiler_base import CompilerBase
from .enaml_ast import StorageExpr


class VarPool(Atom):
    """ A class for generating private variable names.

    """
    #: The pool of currently used variable names.
    pool = Typed(set, ())

    def new(self):
        """ Get a new private variable name.

        Returns
        -------
        result : str
            An unused variable name.

        """
        var = '_[var_%d]' % len(self.pool)
        self.pool.add(var)
        return var

    def release(self, name):
        """ Return a variable name to the pool.

        Parameters
        ----------
        name : str
            The variable name which is free to be reused.

        """
        self.pool.discard(name)


class BlockCompiler(CompilerBase):
    """ A base class for creating block compilers.

    This class implements common logic for the enamldef and template
    compilers.

    """
    #: A variable name generator.
    var_pool = Typed(VarPool, ())

    #: The name of scope key in local storage.
    scope_key = Constant('_[scope_key]')

    #: The name of the node map in the fast locals.
    node_map = Constant('_[node_map]')

    #: A stack of var names for parent classes.
    class_stack = List()

    #: A stack of var names for parent nodes.
    node_stack = List()

    #: A stack of attr bind names for parent nodes.
    bind_stack = List()

    #: A stack of compiled code objects generated by visitors.
    code_stack = List()

    def load_name(self, name):
        """ Load the given name onto the TOS.

        This method must be implemented by subclasses.

        """
        raise NotImplementedError

    def local_names(self):
        """ Get the set of local block names available to user code.

        This method must be implemented by subclasses.

        """
        raise NotImplementedError

    def prepare_block(self):
        """ Prepare the block for execution.

        This method must be invoked by subclasses.

        """
        cg = self.code_generator
        cg.store_globals_to_fast()
        cg.store_helpers_to_fast()
        cg.load_helper_from_fast('make_object')
        cg.call_function()
        cg.store_fast(self.scope_key)
        cg.build_map()
        cg.store_fast(self.node_map)

    def safe_eval_ast(self, ast, name, lineno):
        """ Evaluate an expression in a separate local scope.

        This generates code using the same technique as a Python
        generator expression. It allows the expression to be evaluated
        without the possibility of polluting the local namespace.

        Parameters
        ----------
        ast : ast.Expression
            A Python expression ast node.

        name : str
            The name to use for the code object.

        lineno : int
            The first line number of the expression.

        """
        cg = self.code_generator

        # Generate the code object for the expression
        expr_cg = CodeGenerator(filename=cg.filename)
        expr_cg.set_lineno(lineno)
        expr_cg.insert_python_expr(ast, trim=False)
        call_args = expr_cg.rewrite_to_fast_locals(self.local_names())
        expr_code = expr_cg.to_code(
            args=call_args, newlocals=True, name=name, firstlineno=lineno
        )

        # Create and invoke the function
        cg.load_const(expr_code)
        cg.make_function()
        for ca in call_args:
            self.load_name(ca)
        cg.call_function(len(call_args))

    def visit_ChildDef(self, node):
        """ The compiler visitor for a ChildDef node.

        """
        cg = self.code_generator

        # Claim the variables needed for the class and construct node
        class_var = self.var_pool.new()
        node_var = self.var_pool.new()

        # Set the line number and load the child class
        cg.set_lineno(node.lineno)
        self.load_name(node.typename)

        # Validate the type of the child
        with cg.try_squash_raise():
            cg.dup_top()
            cg.load_helper_from_fast('validate_declarative')
            cg.rot_two()                            # base -> helper -> base
            cg.call_function(1)                     # base -> retval
            cg.pop_top()                            # base

        # Subclass the child class if needed
        if any(isinstance(item, StorageExpr) for item in node.body):
            cg.load_const(node.typename)
            cg.rot_two()
            cg.build_tuple(1)
            cg.build_map()
            cg.load_global('__name__')
            cg.load_const('__module__')
            cg.store_map()                          # name -> bases -> dict
            cg.build_class()                        # class

        # Store the class as a local
        cg.dup_top()
        cg.store_fast(class_var)

        # Build the construct node
        cg.load_helper_from_fast('declarative_node')
        cg.rot_two()
        cg.load_const(node.identifier)
        cg.load_fast(self.scope_key)                # helper -> class -> identifier -> key
        cg.call_function(3)                         # node
        cg.store_fast(node_var)

        #: Store the node in the node map if needed.
        if node.identifier:
            cg.load_fast(self.node_map)
            cg.load_fast(node_var)
            cg.load_const(node.identifier)
            cg.store_map()
            cg.pop_top()

        # Populate the body of the node
        self.class_stack.append(class_var)
        self.node_stack.append(node_var)
        for item in node.body:
            self.visit(item)
        self.class_stack.pop()
        self.node_stack.pop()

        # Add this node to the parent node
        cg.load_fast(self.node_stack[-1])
        cg.load_attr('children')
        cg.load_attr('append')
        cg.load_fast(node_var)
        cg.call_function(1)
        cg.pop_top()

        # Release the held variables
        self.var_pool.release(class_var)
        self.var_pool.release(node_var)

    def visit_TemplateInst(self, node):
        """ The compiler visitor for a TemplateInst node.

        """
        cg = self.code_generator
        cg.set_lineno(node.lineno)

        # Load and validate the template
        self.load_name(node.name)
        with cg.try_squash_raise():
            cg.load_helper_from_fast('validate_template')
            cg.rot_two()
            cg.call_function(1)

        # Load the arguments for the instantiation call
        arguments = node.arguments
        for arg in arguments.args:
            self.safe_eval_ast(arg.ast, node.name, arg.lineno)
        if arguments.stararg:
            arg = arguments.stararg
            self.safe_eval_ast(arg.ast, node.name, arg.lineno)

        # Instantiate the template
        argcount = len(arguments.args)
        varargs = bool(arguments.stararg)
        if varargs:
            cg.call_function_var(argcount)
        else:
            cg.call_function(argcount)

        # Validate the instantiation size, if needed.
        names = ()
        starname = ''
        identifiers = node.identifiers
        if identifiers is not None:
            names = tuple(identifiers.names)
            starname = identifiers.starname
            with cg.try_squash_raise():
                cg.load_helper_from_fast('validate_unpack_size')
                cg.rot_two()
                cg.load_const(len(names))
                cg.load_const(bool(starname))
                cg.call_function(3)

        # Load and call the helper to create the compiler node
        cg.load_helper_from_fast('template_inst_node')
        cg.rot_two()
        cg.load_const(names)
        cg.load_const(starname)
        cg.call_function(3)

        # Append the node to the parent node
        cg.load_fast(self.node_stack[-1])
        cg.load_attr('children')
        cg.load_attr('append')
        cg.rot_two()
        cg.call_function(1)
        cg.pop_top()

    def visit_StorageExpr(self, node):
        """ The compiler visitor for a StorageExpr node.

        """
        cg = self.code_generator
        cg.set_lineno(node.lineno)
        with cg.try_squash_raise():
            cg.load_helper_from_fast('add_storage')
            cg.load_fast(self.class_stack[-1])
            cg.load_const(node.name)
            if node.typename:
                self.load_name(node.typename)
            else:
                cg.load_const(None)
            cg.load_const(node.kind)                # helper -> class -> name -> type -> kind
            cg.call_function(4)                     # retval
            cg.pop_top()

        # Handle the expression binding, if present
        if node.expr is not None:
            self.bind_stack.append(node.name)
            self.visit(node.expr)
            self.bind_stack.pop()

    def visit_Binding(self, node):
        """ The compiler visitor for a Binding node.

        """
        self.bind_stack.append(node.name)
        self.visit(node.expr)
        self.bind_stack.pop()

    def visit_OperatorExpr(self, node):
        """ The compiler visitor for an OperatorExpr node.

        """
        cg = self.code_generator
        self.visit(node.value)
        code = self.code_stack.pop()
        cg.set_lineno(node.lineno)
        with cg.try_squash_raise():
            cg.load_helper_from_fast('run_operator')
            cg.load_fast(self.node_stack[-1])
            cg.load_const(self.bind_stack[-1])
            cg.load_const(node.operator)
            cg.load_const(code)
            cg.load_globals_from_fast()             # helper -> node -> name -> op -> code -> globals
            cg.call_function(5)
            cg.pop_top()

    def visit_PythonExpression(self, node):
        """ The compiler visitor for a PythonExpression node.

        """
        cg = self.code_generator
        code = compile(node.ast, cg.filename, mode='eval')
        self.code_stack.append(code)

    def visit_PythonModule(self, node):
        """ The compiler visitor for a PythonModule node.

        """
        cg = self.code_generator
        code = compile(node.ast, cg.filename, mode='exec')
        self.code_stack.append(code)
