#!/usr/bin/env python

import logging
import plyj.parser as plyj
import sys
import os
import json
import inspect
import re
import keyword

from VariableManager import VariableManager
import SchemeBuilder
import JavaLib
import IAdaptor
import Includer
import DeferClassManager
from Helper import *

import Config

logger = logging.getLogger(__name__)

INITIAL_CLASS = "BasicObject"
SELF_INSTANCE = "_SELF"

""" decorators """
def itemFilter(func):
    """ hook on some function that will generate 'single' name,
    such as : variable name, parameter, hard-code string
    """
    def replaceReservedWord(*args, **kargs):
        _result = func(*args, **kargs)
        reservedWord = {
                "null"    : "None",
                "false"   : "False",
                "true"    : "True",
                "this"    : SELF_INSTANCE,
            }
        if  _result in reservedWord:
            _result = reservedWord[_result]
        _result = keywordReplace_helper(_result)

        """docstring for replaceReservedWord"""
        return _result
    return replaceReservedWord

def scoped(function):
    def enterScope(*args, **kargs):
        self = args[0]
        body = args[1]
        self.indent(body)
        result = function(*args, **kargs)
        self.unindent(body)
        return result
    return enterScope

def loop(fun):
    """Enter a loop"""
    def enterLoop(*args, **kargs):
        self = args[0]
        body = args[1]
        if  hasattr(self, "loopUpdate"):
            self.loopStack.append(self.loopUpdate)

        if  hasattr(body, "update") and body.update:
            self.loopUpdate = body.update
        else:
            self.loopUpdate = None
        result = fun(*args, **kargs)
        del self.loopUpdate
        if  len(self.loopStack) > 0:
            self.loopUpdate = self.loopStack.pop()
        return result
    return enterLoop

class Compiler(object):
    """Java to Python compiler"""
    def __init__(self, fd=None, sBuilder=None, indent="    ", vManager=None):
        logger.debug("Create compiler with args: fd:{}, indent:<{}>, vManager:{}".format(
            "Yes" if fd else "No", indent, "Yes" if vManager else " No"
            ))
        self.fd = fd
        self.outputBuffer = ""
        self.sBuilder = sBuilder
        self.indentPattern = indent
        self.level = 0

        if  vManager:
            self.vManager = vManager # function symbol manager
        else:
            self.vManager = VariableManager() # variable symbol manager
        self.managers = []
        self.managers.append(self.vManager)
        self.mainFunction = None
        self.loopStack = []
        self.deferExpression = []
        self.inCondition = False
        self.iAdaptor = IAdaptor.IncludeAdaptor()
        self.vManager.setIAdaptor(self.iAdaptor)

        # used by instance, function ...
        self.usedName = set()
        self.totalUsed = set()
        self.fieldUsedName = set()

        # self extend graph
        self.deferManager = DeferClassManager.DeferClassManager(self.vManager)

    # Compiler Utilitie
    def c(self, fmt):
        if  self.fd is not None:
            self.fd.write("{}# {}\n".format(self.indentPattern*(self.level), fmt))
        return

    def p(self, fmt, offset=0):
        indents =  self.indentPattern * (self.level + offset)
        result = ""

        if  len(self.usedName) > 0:
            more = self.iAdaptor.getMore(self.usedName) - self.iAdaptor.getInherits()
            if  len(more) > 0:
                for imp in more:
                    result += "{}from {} import *\n".format(indents, imp)

        result += indents + fmt
        while( self.deferExpression):
            indents =  self.indentPattern * (self.level)
            result += indents + self.deferExpression.pop()

        if  self.fd is not None:
            self.fd.write(result)
        else:
            self.outputBuffer += result
        self.totalUsed = self.totalUsed.union(self.usedName)
        self.usedName = set()

    def indent(self, body, **kargs):
        self.level += 1
        for manager in self.managers:
            manager.newScope(body, **kargs)

    def unindent(self, body, **kargs):
        self.level -= 1
        for manager in self.managers:
            manager.leaveScope(body, **kargs)

    def preprocess(self, body):
        SchemeBuilder.buildHelper(body, self.vManager)

    def compile(self, body):
        """ entry function """
        self.preprocess(body)
        self.solver(body)
       
        while(not self.deferManager.isEmpty()):
            for cls in self.deferManager.sort():
                self.vManager.setSnapshot(cls.snapshot)
                if  cls.mtype == DeferClassManager.CLASS:
                    self.solver(cls.obj, absExtends=True)
                    self.p("{} = {}\n".format(self.vManager.findClass(cls.name), cls.name))
                elif cls.mtype == DeferClassManager.ANONYMOUS:
                    oClass, variable, initializer = cls.obj
                    self.solver(oClass)
                    self.p("{} = {}\n".format(variable, initializer))
        if  self.mainFunction:
            self.p("if __name__ == '__main__':\n")
            self.p("    import sys\n")
            self.p("    {}(sys.argv)\n".format(self.mainFunction))
        return self.outputBuffer

    def compilePackage(self, root, filePath):
        parser = plyj.Parser().parse_file(filePath)

        includer = Includer.Includer(root, filePath)
        self.iAdaptor.setIncluder(includer)
        result = self.compile(parser)
        dependsPkgs = self.iAdaptor.getInherits()
        dependsPkgs = dependsPkgs.union(self.iAdaptor.getMore(self.fieldUsedName))
        self.imports = includer.getMore(self.totalUsed).union(dependsPkgs)
            
        builtinImports = [
            "from lib.Switch import Switch\n",
            "from lib.BasicObject import BasicObject\n",
        ]
        prefix = "".join(builtinImports) + "".join(["from {} import *\n".format(pkg) for pkg in dependsPkgs])
        return prefix + result
    
    def CompilationUnit(self, body):
        package_declaration = self.solver(body.package_declaration)
        for importer in body.import_declarations:
            self.solver(importer)

        for typer in body.type_declarations:
            self.solver(typer)

    def PackageDeclaration(self, body):
        name = self.solver(body.name)
        self.iAdaptor.setPackage( name)
        self.c("package {}\n".format(name))

    def ImportDeclaration(self, body):
        name = self.solver(body.name)
        self.iAdaptor.addImport(name, body.static)
        self.c("import {}\n".format(name))

    @scoped
    def InterfaceDeclaration(self, body, absExtends=False):
        name, implements, decorators = getInterfaceScheme_helper(body, self.solver, self.vManager)

        tmp = set()
        for impl in implements:
            try:
                self.vManager.addInherit(impl)
            except Includer.NonIncludeClass as e:
                tmp.add(impl)
                logger.warn(e)
        map(implements.remove, tmp)

        if  absExtends:
            for i in range(len(implements)):
                if  self.vManager.findClass(implements[i]):
                    implements[i] = self.vManager.getFullPathByName(implements[i])

        if  len(implements) == 0:
            implements.append(INITIAL_CLASS)
        self.c("# interface")
        self.p("class {name}({parent}):\n".format(name = name, parent = ", ".join(implements)), offset=-1)
        if  not body.body or len(body.body) == 0:
            self.p("pass\n")
            return

        # Field => Functions => Classes
        # -----------------------------
        # field process
        tmp = set()
        #body preprocess
        function_methods = set()
        overloading = []

        for comp in body.body:
            if  type(comp) == plyj.FieldDeclaration:
                self.solver(comp)
            elif  type(comp) == plyj.MethodDeclaration or type(comp) == plyj.ConstructorDeclaration:
                functionName = self.solver(comp.name)
                overloading.append(functionName) if functionName in function_methods else function_methods.add(functionName)
            elif type(comp) == plyj.ClassDeclaration:
                subName, subImplements, subDecorators = getClassScheme_helper(comp, self.solver, self.vManager)
                depends = deferImplement_helper(self.vManager, subImplements)
                self.deferManager.addClass(subName, depends, comp)
            elif type(comp) == plyj.InterfaceDeclaration:
                subName, subImplements, subDecorators = getInterfaceScheme_helper(comp, self.solver, self.vManager)
                depends = deferImplement_helper(self.vManager, subImplements)
                if  len(depends) > 0:
                    self.classGraph[subName] = depends
                    self.outsideClasses[subName] = comp
                else:
                    self.solver(comp)
            else:
                self.solver(comp)

        for comp in body.body:
            if  type(comp) == plyj.MethodDeclaration or type(comp) == plyj.ConstructorDeclaration:
                functionName = self.solver(comp.name)
                if  functionName in overloading:
                    self.solver(comp, appendName=True)
                else:
                    self.solver(comp)

    @scoped
    def ClassDeclaration(self, body, absExtends=False):
        name, implements, decorators = getClassScheme_helper(body, self.solver, self.vManager)

        tmp = set()
        for impl in implements:
            try:
                self.vManager.addInherit(impl)
            except Includer.NonIncludeClass as e:
                tmp.add(impl)
                logger.warn(e)
        map(implements.remove, tmp)

        if  absExtends:
            for i in range(len(implements)):
                if  self.vManager.findClass(implements[i]):
                    implements[i] = self.vManager.getFullPathByName(implements[i])
        if  len(implements) == 0:
            implements.append(INITIAL_CLASS)

        self.p("class {name}({parent}):\n".format(name = name, parent = ", ".join(implements)), offset=-1)

        if  len(body.body) == 0:
            self.p("pass\n")
            return
        elif type(body.body[0]) == plyj.ClassInitializer:
            self.p("pass\n")
            return

        # Field => Functions => Classes
        # -----------------------------
        # field process
        tmp = set()
        #body preprocess
        function_methods = set()
        overloading = []

        # first step scanning
        for comp in body.body:
            if  type(comp) == plyj.FieldDeclaration:
                self.solver(comp)
            elif  type(comp) == plyj.MethodDeclaration or type(comp) == plyj.ConstructorDeclaration:
                self.vManager.newScope(comp)
                self.vManager.leaveScope(comp)
                functionName = self.solver(comp.name)
                overloading.append(functionName) if functionName in function_methods else function_methods.add(functionName)
            elif type(comp) == plyj.ClassDeclaration:
                subName, subImplements, subDecorators = getClassScheme_helper(comp, self.solver, self.vManager)
                depends = deferImplement_helper(self.vManager, subImplements)
                self.deferManager.addClass(subName, depends, comp)
            elif type(comp) == plyj.InterfaceDeclaration:
                subName, subImplements, subDecorators = getInterfaceScheme_helper(comp, self.solver, self.vManager)
                depends = deferImplement_helper(self.vManager, subImplements)
                if  len(depends) > 0:
                    self.classGraph[subName] = depends
                    self.outsideClasses[subName] = comp
                else:
                    self.solver(comp)
            else:
                self.solver(comp)

        # TODO: remove outsided classes

        # append function overload entry
        if  len(overloading) > 0:
            temp = set(overloading)
            if  name in temp:
                    temp.remove(name)
                    temp.add("__init__")
            self.overloadEntry(temp)

        for comp in body.body:
            if  type(comp) == plyj.MethodDeclaration or type(comp) == plyj.ConstructorDeclaration:
                functionName = self.solver(comp.name)
                if  functionName in overloading:
                    self.solver(comp, appendName=True)
                else:
                    self.solver(comp)

    def ClassInitializer(self, body):
        return
        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        for i in range(2,8):
            logger.info(calframe[i][3])
        self.solver(body.block)

    def EmptyDeclaration(self, body):
        return 

    def FieldDeclaration(self, body):
        mtype = self.solver(body.type)
        variable_declarators = []
        for var in body.variable_declarators:
            if  var.initializer and type(var.initializer) == plyj.InstanceCreation and len(var.initializer.body) > 0:
                mtype = self.solver(var.initializer.type)
                variable = self.solver(var.variable)
                anonymous = AnonymousName_helper()
                oClass = plyj.ClassDeclaration(anonymous, var.initializer.body, extends=mtype)
                initializer = plyj.InstanceCreation(
                        anonymous,
                        type_arguments=var.initializer.type_arguments,
                        arguments=var.initializer.arguments,
                        enclosed_in=var.initializer.enclosed_in)
                self.deferManager.addAnonyClass(anonymous, [mtype], (oClass, self.vManager.getPath() + "." + variable, self.solver(initializer)))
                return

            variable, initializer = self.solver(var)
            self.vManager.newVariable(variable, mtype, isMember=True)
            if  initializer is None:
                self.c(mtype)
                result = "{} = {}\n".format(variable, JavaLib.builtinTypes(mtype))
            elif initializer.startswith(ANONYMOUS_PREFIX) or type(var.initializer) in [plyj.Literal, plyj.Unary]:
                if  type(var.initializer) == plyj.Name:
                    for part in initializer.split("."):
                        self.fieldUsedName.add(part)
                    if  initializer in self.vManager.members:
                        initializer = self.vManager.members[initializer]
                result = "{} = {}\n".format(variable, initializer)
            else:
                result = "{} = None\n".format(variable)
            self.p(result)

    def FieldAccess(self, body):
        return "{}.{}".format(SELF_INSTANCE, self.solver(body.name))

    def ConstructorDeclaration(self, body, appendName = False):
        body.name = "__init__"
        if  appendName:
            funcName = self._getOverrideName("__init__", body.parameters)
            body.name = funcName
        setattr(body, "body", body.block)
        self._classMethodDeclaration(body)

    def ConstructorInvocation(self, body):
        arguments = []
        for arg in body.arguments:
            sArg = self.solver(arg)
            arguments.append(sArg)

        clsName = self.vManager.getPath().split(".")[-2]
        self.p("{}({})\n".format(clsName, ", ".join(arguments)))

    def MethodDeclaration(self, body, appendName = False):
        name = self.solver(body.name)
        if  appendName:
            funcName = self._getOverrideName(name, body.parameters)
            body.name = funcName
        self._classMethodDeclaration(body)

    @scoped
    def EnumDeclaration(self, body):
        name = self.solver(body.name)
        self.p("class {}:\n".format(name), offset =-1)

        for stmt in body.body:
            if  type(stmt) == plyj.EnumConstant:
                self.solver(stmt)

    def EnumConstant(self, body):
        name = self.solver(body.name)
        self.p("{name} = \"{name}\"\n".format(name=name))

    def _getOverrideName(self, funcName, parameters):
        args_type = []
        for arg in parameters:
            name, mtype = self.solver(arg)
            args_type.append(mtype.split(".")[-1]) # get the last type name of parameters
        return self._overrideName(funcName, args_type)


    def _overrideName(self, funcName, args_type):
        return "Oed_{}__{}".format(funcName, "__".join(args_type))

    @scoped
    def _classMethodDeclaration(self, body):

        functionName = self.solver(body.name)
        if  functionName == "main":
            self.mainFunction = self.vManager.getPath()
        """
        elif functionName == "toString":
            functionName = "__str__"
        """

        args = [SELF_INSTANCE]
        for arg in body.parameters:
            name, mtype = self.solver(arg)
            args.append(name)

        self.p("@classmethod\n", offset=-1)
        self.p("def {}({}):\n".format(functionName, ", ".join(args)), offset = -1)

        if  not body.body or len(body.body) == 0:
            self.p("pass\n")
            return
        for stmt in body.body:
            result = self.solver(stmt)
            if  result:
                self.p(result + "\n")
        self.p("import __builtin__\n")
        self.p("__builtin__.methodPacCalledCount+=1\n")

    def AnnotationDeclaration(self, body):
        return

    def Throw(self, body):
        """
        Throw(exception=InstanceCreation(type=Type(name=Name(value='IllegalArgumentException'), type_arguments=[], enclosed_in=None, dimensions=0), type_arguments=[], arguments=[Additive(operator='+', lhs=Additive(operator='+', lhs=Literal(value='"Invalid character "'), rhs=Name(value='nibble')), rhs=Literal(value='" in hex string"'))], body=[], enclosed_in=None))
        """
        if  hasattr(body.exception, "arugments"):
            result = ""
            for args in body.exception.arguments:
                result += self.solver(args)
        else:
            result = self.solver(body.exception)
        result = result.replace("\"", "'")
        self.p("raise Exception(\"{}\")\n".format(result))

    def Throws(self, body):
        throws = []
        for mtype in body.types:
            throws.append(self.solver(mtype))
        return throws


    #@scoped
    def Synchronized(self, body):
        #self.p("synchronized({})\n".format(body.monitor), offset=-1)
        self.solver(body.body)
        
    @scoped
    def IfThenElse(self, body):
        predicate = self.solver(body.predicate, inCondition = True)
        self.p("if {}:\n".format(predicate), offset=-1)
        if  body.if_true == None:
            self.p("pass\n")
        else:
            result = self.solver(body.if_true)
            self.p("{}\n".format(result)) if result else None
        
        if  body.if_false != None:
            self.p("else:\n", offset=-1)
            result = self.solver(body.if_false)
            self.p("{}\n".format(result)) if result else None

    @scoped
    def Switch(self, body):

        value = self.solver(body.expression)
        cases = body.switch_cases
        self.p("for mycase in Switch({}):\n".format(value), offset=-1)
        for case in cases:
            self.solver(case)

    @scoped
    def SwitchCase(self, body):
        cases = []
        for case in body.cases:
            case = self.solver(case)
            case = self.vManager.decorate(case, SELF_INSTANCE)
            cases.append(case)

        if  cases[0] == "default":
            self.p("if mycase():\n", offset=-1)
        else:
            self.p("if {}:\n".format(" or ".join("mycase(" + i + ")" for i in cases)), offset=-1)

        if  len(body.body) == 0:
            self.p("pass\n")
        for comp in body.body:
            result = self.solver(comp)
            if  result:
                self.p(result + "\n")

    def Block(self, body):
        stmts = body.statements
        if  len(stmts) == 0:
            self.p("pass\n")
        for stmt in stmts:
            stmt = self.solver(stmt)
            if  stmt:
                self.p(stmt + "\n")

    def Try(self, body):
        self.solver(body.block)

    @scoped
    @loop
    def While(self, body):
        self.p("while {}:\n".format(self.solver(body.predicate, inCondition=True) if body.predicate else "True") , offset=-1)
        if  body.body:
            result = self.solver(body.body)
            if type(result) == str:
                self.p(result + "\n")
        else:
            self.p("pass\n")

    @scoped
    @loop
    def DoWhile(self, body):
        self.p("while True:\n", offset=-1)
        result = self.solver(body.body)
        if  result:
            self.p(result + "\n")
        self.p("if not ({}):\n".format(self.solver(body.predicate, inCondition=True)))
        self.p("break\n", offset=1)

    @scoped
    @loop
    def For(self, body):
        if body.init:
            if  type(body.init) == list:
                for init in body.init:
                    self.p(self.solver(init) + "\n", offset =-1)
            else:
                self.p(self.solver(body.init) + "\n", offset =-1)
        self.p("while {}:\n".format(self.solver(body.predicate, inCondition=True) if body.predicate else "True") , offset=-1)
        self.solver(body.body)
        if  not body.update:
            return
        for update in body.update:
            self.p(self.solver(update) + "\n")

    @scoped
    @loop
    def ForEach(self, body):
        self.p("for {} in {}:\n".format(self.solver(body.variable), self.solver(body.iterable)) , offset=-1)
        if  body.body:
            result = self.solver(body.body)
            if  type(result) == str:
                self.p(result + "\n")

    def Break(self, body):
        return "break"

    def Continue(self, body):
        if  self.loopUpdate:
            for update in self.loopUpdate:
                self.p(self.solver(update) + "\n")
        return "continue"

    def Statements(self, body):
        raise Undefined

    def Statement(self, body):
        raise Undefined

    def Assert(self, body):
        return 
    
    def Assignment(self, body):
        # Assignment(operator='=', lhs=Name(value='_arg1'), rhs=MethodInvocation(name='readInt', arguments=[], type_arguments=[], target=Name(value='data')))
        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        caller = calframe[2][3]
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        if  operator == ">>>=":
            operator = "="
            rhs = "{} >> {}".format(lhs, rhs)
        if  self.inCondition:
            self.p("{lhs} {op} {rhs}\n".format(lhs = lhs, op = operator, rhs = rhs), offset = -1)
            return lhs
        elif  caller in ['Assignment', "Equality", "Relational", "Return", "Unary", "Conditional"] :
            offset = 0
            self.p("{lhs} {op} {rhs}\n".format(lhs = lhs, op = operator, rhs = rhs), offset = offset)
            return lhs

        return "{lhs} {op} {rhs}".format(lhs = lhs, op = operator, rhs = rhs)


    def Return(self, body):
        return "return {}".format(self.solver(body.result))

    def Annotation(self, body):
        return self.solver(body.name)

    def Type(self, body):
        tArgs = []
        for arg in body.type_arguments:
            tArgs.append(self.solver(arg))
        type_arguments = ""
        if  body.enclosed_in:
            enclosed = self.solver(body.enclosed_in) + "."
        else:
            enclosed = ""
        dimensions=body.dimensions
        name = self.solver(body.name)
        if  name in JavaLib.builtinMap:
            name = JavaLib.builtinMap[name]
        return "{}{}{}".format(enclosed, name, type_arguments )

    def Conditional(self, body):
        predicate = self.solver(body.predicate, inCondition=True)
        if_true = self.solver(body.if_true)
        if_false = self.solver(body.if_false)
        return "( {if_true} if {predicate} else {if_false} )".format(if_true=if_true, predicate=predicate, if_false = if_false)

    def ClassLiteral(self, body):
        return "{}.__class__".format(self.solver(body.type))

    @itemFilter
    def Name(self, body):
        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        caller = calframe[3][3]
        value = body.value
        if  caller not in ["ImportDeclaration", "PackageDeclaration", "Type"]:
            for component in value.split("."):
                self.usedName.add(component)
        if  caller == "Type" :
            return value
        value = self.vManager.decorate(value, SELF_INSTANCE)
        return value
        
    @itemFilter
    def Literal(self, body):
        value = body.value
        if  re.match(r'[\d\.]+[fF]$', value):
            value = str(float(value[:-1]))
        return value

    @itemFilter
    def Variable(self, body):
        return self.solver(body.name)

    def FormalParameter(self, body):
        """
        variable=<class 'plyj.model.Variable'>
        type=<class 'plyj.model.Type'>
        modifiers=<type 'list'>
        vararg=<type 'bool'>
        """
        variable = self.solver(body.variable)
        mtype = self.solver(body.type)
        self.vManager.newVariable(variable, mtype)
        if  hasattr(body.type, "dimensions") and body.type.dimensions > 0:
            mtype = "list"
        return variable, mtype

    def InstanceCreation(self, body, isAnonymous=False):
        collector = set()
        mtype = self.solver(body.type)
        map(self.usedName.add, mtype.split("."))

        #built-in types
        if  mtype == "Object":
            mtype = INITIAL_CLASS


        args = []
        for arg in body.arguments:
            arg = self.solver(arg)
            args.append(self.solver(arg))
        if  len(body.body) > 0: # anonymous function
            anonymous = AnonymousName_helper()
            oClass = plyj.ClassDeclaration(anonymous, body.body, extends=body.type)
            initializer = plyj.InstanceCreation( anonymous, type_arguments=body.type_arguments, arguments=body.arguments, enclosed_in=body.enclosed_in)
            raise ClassOverriding(oClass, initializer)
        else:
            return "{}({})".format(mtype, ", ".join(args))

    @scoped
    def OverloadingInstance(self, variableDeclarator):
        variable = self.solver(variableDeclarator.variable)
        args = []
        instanceCreation = variableDeclarator.initializer
        for arg in instanceCreation.arguments:
            args.append(self.solver(arg))
        self.p("class {}({}):\n".format(variable, ", ".join(args)), offset=-1)
        for stmt in instanceCreation.body:
            self.solver(stmt)

    def VariableDeclaration(self, body):
        """
        TYPE := VARIABLE_DECLARATORS
        """
        mtype = self.solver(body.type)
        variables = []
        initializers = []
        for variable in body.variable_declarators:
            variable, initializer = self.solver(variable)
            if  variable:
                self.vManager.newVariable(variable, mtype)
            if  initializer:
                variables.append(variable)
                initializers.append(initializer)
            else:
                variables.append(variable)
                initializers.append(JavaLib.builtinTypes(mtype))

        return "{} = {}".format(", ".join(variables), ", ".join(initializers))

    def VariableDeclarator(self, body):
        variable=self.solver(body.variable)
        initializer = self.solver(body.initializer)
        return variable, initializer

    @JavaLib.method
    def MethodInvocation(self, body):
        name = self.solver(body.name)

        arguments = body.arguments
        args = []
        for arg in arguments:
            _result = self.solver(arg)
            args.append(_result)


        type_arguments = body.type_arguments

        if body.target is None:
            name = self.vManager.decorate(name, SELF_INSTANCE)
            return "{name}({args})".format(name = name, args = ", ".join(args))
        
        targets = self.solver(body.target).split(".")
        # IIntentReceiver.Stub.asInterface
        if  name == "asInterface" and targets[0][0] == "I" and targets[1] == "Stub": 
            #return strongBinder
            return "{}.asInterface(\"{}\")".format(args[0], ".".join(targets[:2]))

        if targets[0] == "this":
            targets[0] = SELF_INSTANCE
        elif targets[0] == "super":
            clsName = self.vManager.getPath().split(".")[-2]
            targets[0] = "super({}, {})".format(clsName, SELF_INSTANCE)
        else:
            targets[0] = self.vManager.decorate(targets[0], SELF_INSTANCE)
        return "{}.{}({})".format(".".join(keywordReplace_helper(i) for i in targets), name, ", ".join(args))

    def Wildcard(self, body):
        return 

    def InstanceOf(self, body):
        return "isinstance({}, {})".format(self.solver(body.lhs), self.solver(body.rhs))

    def ConditionalOr(self, body):
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} or {})".format(lhs, rhs)

    def ConditionalAnd(self, body):
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} and {})".format(lhs, rhs)

    def And(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Or(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Xor(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Multiplicative(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)
    
    def Equality(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Relational(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Additive(self, body):
        operator = self.solver(body.operator)
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        if type(body.lhs) == plyj.Literal and lhs.find("E") > 0:
            return "{}{}{}".format(lhs, operator, rhs)
        return "({} {} {})".format(lhs, operator, rhs)
    
    def Unary(self, body):
        """
        sign=<type 'str'>
        expression=<class 'plyj.model.Literal'>
        """
        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        caller = calframe[2][3]
        sign = body.sign
        expression = self.solver(body.expression)

        if  caller not in ["Block", "IfThenElse", "While", "For", "DoWhile", "SwitchCase", "_classMethodDeclaration"]:
            if  self.inCondition:
                offset = -1
            else:
                offset = 0

            if  sign == "x++":
                self.deferExpression.append("{} += 1\n".format(expression))
            elif  sign == "x--":
                self.deferExpression.append("{} -= 1\n".format(expression))
            elif  sign == "++x":
                self.p("{} += 1\n".format(expression), offset=offset)
            elif sign == "--x":
                self.p("{} -= 1\n".format(expression), offset=offset)
            return expression

        if  sign == "x++":
            return "{} += 1".format(expression)
        elif  sign == "x--":
            return "{} -= 1".format(expression)
        elif    sign == "!":
            return "not {}".format(expression)
        return "{}{}".format(sign, expression)

    def Shift(self, body):
        operator = self.solver(body.operator)
        if  operator == ">>>":
            operator = ">>"
        lhs = self.solver(body.lhs)
        rhs = self.solver(body.rhs)
        return "({} {} {})".format(lhs, operator, rhs)

    def Cast(self, body):
        return self.solver(body.expression)

    def Empty(self, body):
        return "pass"

    def ArrayInitializer(self, body):
        return "[{}]".format(", ".join(self.solver(i) for i in body.elements))

    def ArrayCreation(self, body):
        # ArrayCreation(type='int', dimensions=[Name(value='_arg4_length')], initializer=None)
        mtype = self.solver(body.type)
        dims = []
        for dim in body.dimensions:
            dims.append(self.solver(dim))
        dimensions = "".join("[{}()]*{}".format(mtype, i) for i in dims)
        initializer = body.initializer
        self.c(mtype)
        return "{dimensions}".format(dimensions = dimensions)
    
    def ArrayAccess(self, body):
        index = self.solver(body.index)
        target = self.solver(body.target)
        return "{}[{}]".format(target, index)

    def solver(self, thing, **kargs):
        if  thing == None:
            return thing
        if  type(thing) == str:
            if  thing in JavaLib.builtinMap:
                thing = JavaLib.builtinMap[thing]
            if  thing == "this":
                thing = SELF_INSTANCE
            if  thing.find("$") > 0:
                thing = thing.replace("$", "_D")
            return keywordReplace_helper(thing)
        if  type(thing) == list:
            return thing

        oldCondition = self.inCondition
        if  "inCondition" in kargs:
            self.inCondition = kargs['inCondition']
            del kargs["inCondition"]

        try:
            result = getattr(self, thing.__class__.__name__)(thing, **kargs)
        except ClassOverriding as e:
            oClass = self.solver(e.args[0])
            result = self.solver(e.args[1], isAnonymous=True)

        self.inCondition = oldCondition
        return result

    def overloadEntry(self, overloading):
        self.c("Overloading Entries")
        clsName = self.vManager.getPath()
        for method in overloading:
            self.p("\n")
            self.p("@classmethod\n")
            self.p("def {}({}, *args):\n".format(method, SELF_INSTANCE))
            self.p("    fname = \"Oed_{}__\" + \"__\".join(i.__class__.__name__ for i in args)\n".format(method))
            self.p("    func = getattr({}, fname)\n".format(clsName))
            self.p("    return func(*args)\n")

         
class Undefined(Exception):
    pass

class NotFound(Exception):
    pass

class Continue(Exception):
    pass

class ClassOverriding(Exception):
    pass


def dumper(body, stop = False):
    if hasattr(body, "_fields"):
        for attr in body._fields:
            print "{}={}".format(attr, type(getattr(body, attr)))
    else:
        print body
    if  stop:
        print "end"
        exit()

if __name__ == '__main__':
    logging.basicConfig(level = logging.INFO)
    
    root = "/home/user/android-5.1.1_r1/frameworks/base/core/java"
    inputPath = "/home/user/android-5.1.1_r1/frameworks/base/core/java/android/os/StrictMode.java"
#   inputPath = "/home/user/android-5.1.1_r1/frameworks/base/telecomm/java/android/telecom/PhoneAccountHandle.java"
    with open(inputPath, "r") as inputFd:
        compiler = Compiler(sys.stdout)
        print compiler.compilePackage(root, inputPath)
        imports = compiler.imports
