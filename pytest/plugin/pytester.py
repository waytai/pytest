"""
funcargs and support code for testing py.test's own functionality.
"""

import py
import sys, os
import re
import inspect
import time
from fnmatch import fnmatch
from pytest.plugin.session import Collection
from py.builtin import print_
from pytest._core import HookRelay

def pytest_addoption(parser):
    group = parser.getgroup("pylib")
    group.addoption('--no-tools-on-path',
           action="store_true", dest="notoolsonpath", default=False,
           help=("discover tools on PATH instead of going through py.cmdline.")
    )

def pytest_funcarg___pytest(request):
    return PytestArg(request)

class PytestArg:
    def __init__(self, request):
        self.request = request

    def gethookrecorder(self, hook):
        hookrecorder = HookRecorder(hook._pm)
        hookrecorder.start_recording(hook._hookspecs)
        self.request.addfinalizer(hookrecorder.finish_recording)
        return hookrecorder

class ParsedCall:
    def __init__(self, name, locals):
        assert '_name' not in locals
        self.__dict__.update(locals)
        self.__dict__.pop('self')
        self._name = name

    def __repr__(self):
        d = self.__dict__.copy()
        del d['_name']
        return "<ParsedCall %r(**%r)>" %(self._name, d)

class HookRecorder:
    def __init__(self, pluginmanager):
        self._pluginmanager = pluginmanager
        self.calls = []
        self._recorders = {}

    def start_recording(self, hookspecs):
        if not isinstance(hookspecs, (list, tuple)):
            hookspecs = [hookspecs]
        for hookspec in hookspecs:
            assert hookspec not in self._recorders
            class RecordCalls:
                _recorder = self
            for name, method in vars(hookspec).items():
                if name[0] != "_":
                    setattr(RecordCalls, name, self._makecallparser(method))
            recorder = RecordCalls()
            self._recorders[hookspec] = recorder
            self._pluginmanager.register(recorder)
        self.hook = HookRelay(hookspecs, pm=self._pluginmanager,
            prefix="pytest_")

    def finish_recording(self):
        for recorder in self._recorders.values():
            self._pluginmanager.unregister(recorder)
        self._recorders.clear()

    def _makecallparser(self, method):
        name = method.__name__
        args, varargs, varkw, default = py.std.inspect.getargspec(method)
        if not args or args[0] != "self":
            args.insert(0, 'self')
        fspec = py.std.inspect.formatargspec(args, varargs, varkw, default)
        # we use exec because we want to have early type
        # errors on wrong input arguments, using
        # *args/**kwargs delays this and gives errors
        # elsewhere
        exec (py.code.compile("""
            def %(name)s%(fspec)s:
                        self._recorder.calls.append(
                            ParsedCall(%(name)r, locals()))
        """ % locals()))
        return locals()[name]

    def getcalls(self, names):
        if isinstance(names, str):
            names = names.split()
        for name in names:
            for cls in self._recorders:
                if name in vars(cls):
                    break
            else:
                raise ValueError("callname %r not found in %r" %(
                name, self._recorders.keys()))
        l = []
        for call in self.calls:
            if call._name in names:
                l.append(call)
        return l

    def contains(self, entries):
        from py.builtin import print_
        i = 0
        entries = list(entries)
        backlocals = py.std.sys._getframe(1).f_locals
        while entries:
            name, check = entries.pop(0)
            for ind, call in enumerate(self.calls[i:]):
                if call._name == name:
                    print_("NAMEMATCH", name, call)
                    if eval(check, backlocals, call.__dict__):
                        print_("CHECKERMATCH", repr(check), "->", call)
                    else:
                        print_("NOCHECKERMATCH", repr(check), "-", call)
                        continue
                    i += ind + 1
                    break
                print_("NONAMEMATCH", name, "with", call)
            else:
                raise AssertionError("could not find %r in %r" %(
                    name, self.calls[i:]))

    def popcall(self, name):
        for i, call in enumerate(self.calls):
            if call._name == name:
                del self.calls[i]
                return call
        raise ValueError("could not find call %r" %(name, ))

    def getcall(self, name):
        l = self.getcalls(name)
        assert len(l) == 1, (name, l)
        return l[0]


def pytest_funcarg__linecomp(request):
    return LineComp()

def pytest_funcarg__LineMatcher(request):
    return LineMatcher

def pytest_funcarg__testdir(request):
    tmptestdir = TmpTestdir(request)
    return tmptestdir

rex_outcome = re.compile("(\d+) (\w+)")
class RunResult:
    def __init__(self, ret, outlines, errlines, duration):
        self.ret = ret
        self.outlines = outlines
        self.errlines = errlines
        self.stdout = LineMatcher(outlines)
        self.stderr = LineMatcher(errlines)
        self.duration = duration

    def parseoutcomes(self):
        for line in reversed(self.outlines):
            if 'seconds' in line:
                outcomes = rex_outcome.findall(line)
                if outcomes:
                    d = {}
                    for num, cat in outcomes:
                        d[cat] = int(num)
                    return d

class TmpTestdir:
    def __init__(self, request):
        self.request = request
        self.Config = request.config.__class__
        self._pytest = request.getfuncargvalue("_pytest")
        # XXX remove duplication with tmpdir plugin
        basetmp = request.config.ensuretemp("testdir")
        name = request.function.__name__
        for i in range(100):
            try:
                tmpdir = basetmp.mkdir(name + str(i))
            except py.error.EEXIST:
                continue
            break
        # we need to create another subdir
        # because Directory.collect() currently loads
        # conftest.py from sibling directories
        self.tmpdir = tmpdir.mkdir(name)
        self.plugins = []
        self._syspathremove = []
        self.chdir() # always chdir
        self.request.addfinalizer(self.finalize)

    def __repr__(self):
        return "<TmpTestdir %r>" % (self.tmpdir,)

    def finalize(self):
        for p in self._syspathremove:
            py.std.sys.path.remove(p)
        if hasattr(self, '_olddir'):
            self._olddir.chdir()
        # delete modules that have been loaded from tmpdir
        for name, mod in list(sys.modules.items()):
            if mod:
                fn = getattr(mod, '__file__', None)
                if fn and fn.startswith(str(self.tmpdir)):
                    del sys.modules[name]

    def getreportrecorder(self, obj):
        if hasattr(obj, 'config'):
            obj = obj.config
        if hasattr(obj, 'hook'):
            obj = obj.hook
        assert hasattr(obj, '_hookspecs'), obj
        reprec = ReportRecorder(obj)
        reprec.hookrecorder = self._pytest.gethookrecorder(obj)
        reprec.hook = reprec.hookrecorder.hook
        return reprec

    def chdir(self):
        old = self.tmpdir.chdir()
        if not hasattr(self, '_olddir'):
            self._olddir = old

    def _makefile(self, ext, args, kwargs):
        items = list(kwargs.items())
        if args:
            source = "\n".join(map(str, args)) + "\n"
            basename = self.request.function.__name__
            items.insert(0, (basename, source))
        ret = None
        for name, value in items:
            p = self.tmpdir.join(name).new(ext=ext)
            source = str(py.code.Source(value)).lstrip()
            p.write(source.encode("utf-8"), "wb")
            if ret is None:
                ret = p
        return ret


    def makefile(self, ext, *args, **kwargs):
        return self._makefile(ext, args, kwargs)

    def makeconftest(self, source):
        return self.makepyfile(conftest=source)

    def makepyfile(self, *args, **kwargs):
        return self._makefile('.py', args, kwargs)

    def maketxtfile(self, *args, **kwargs):
        return self._makefile('.txt', args, kwargs)

    def syspathinsert(self, path=None):
        if path is None:
            path = self.tmpdir
        py.std.sys.path.insert(0, str(path))
        self._syspathremove.append(str(path))

    def mkdir(self, name):
        return self.tmpdir.mkdir(name)

    def mkpydir(self, name):
        p = self.mkdir(name)
        p.ensure("__init__.py")
        return p

    def getnode(self, config, arg):
        collection = Collection(config)
        return collection.getbyid(collection._normalizearg(arg))[0]

    def genitems(self, colitems):
        collection = colitems[0].collection
        result = []
        collection.genitems(colitems, (), result)
        return result

    def inline_genitems(self, *args):
        #config = self.parseconfig(*args)
        config = self.parseconfigure(*args)
        rec = self.getreportrecorder(config)
        items = Collection(config).perform_collect()
        return items, rec

    def runitem(self, source):
        # used from runner functional tests
        item = self.getitem(source)
        # the test class where we are called from wants to provide the runner
        testclassinstance = py.builtin._getimself(self.request.function)
        runner = testclassinstance.getrunner()
        return runner(item)

    def inline_runsource(self, source, *cmdlineargs):
        p = self.makepyfile(source)
        l = list(cmdlineargs) + [p]
        return self.inline_run(*l)

    def inline_runsource1(self, *args):
        args = list(args)
        source = args.pop()
        p = self.makepyfile(source)
        l = list(args) + [p]
        reprec = self.inline_run(*l)
        reports = reprec.getreports("pytest_runtest_logreport")
        assert len(reports) == 1, reports
        return reports[0]

    def inline_run(self, *args):
        args = ("-s", ) + args # otherwise FD leakage
        config = self.parseconfig(*args)
        reprec = self.getreportrecorder(config)
        #config.pluginmanager.do_configure(config)
        config.hook.pytest_cmdline_main(config=config)
        #config.pluginmanager.do_unconfigure(config)
        return reprec

    def config_preparse(self):
        config = self.Config()
        for plugin in self.plugins:
            if isinstance(plugin, str):
                config.pluginmanager.import_plugin(plugin)
            else:
                if isinstance(plugin, dict):
                    plugin = PseudoPlugin(plugin)
                if not config.pluginmanager.isregistered(plugin):
                    config.pluginmanager.register(plugin)
        return config

    def parseconfig(self, *args):
        if not args:
            args = (self.tmpdir,)
        config = self.config_preparse()
        args = list(args) + ["--basetemp=%s" % self.tmpdir.dirpath('basetemp')]
        config.parse(args)
        return config

    def reparseconfig(self, args=None):
        """ this is used from tests that want to re-invoke parse(). """
        if not args:
            args = [self.tmpdir]
        oldconfig = getattr(py.test, 'config', None)
        try:
            c = py.test.config = self.Config()
            c.basetemp = oldconfig.mktemp("reparse", numbered=True)
            c.parse(args)
            return c
        finally:
            py.test.config = oldconfig

    def parseconfigure(self, *args):
        config = self.parseconfig(*args)
        config.pluginmanager.do_configure(config)
        return config

    def getitem(self,  source, funcname="test_func"):
        modcol = self.getmodulecol(source)
        moditems = modcol.collect()
        for item in modcol.collect():
            if item.name == funcname:
                return item
        else:
            assert 0, "%r item not found in module:\n%s" %(funcname, source)

    def getitems(self,  source):
        modcol = self.getmodulecol(source)
        return self.genitems([modcol])

    def getmodulecol(self,  source, configargs=(), withinit=False):
        kw = {self.request.function.__name__: py.code.Source(source).strip()}
        path = self.makepyfile(**kw)
        if withinit:
            self.makepyfile(__init__ = "#")
        self.config = config = self.parseconfigure(path, *configargs)
        node = self.getnode(config, path)
        #config.pluginmanager.do_unconfigure(config)
        return node

    def popen(self, cmdargs, stdout, stderr, **kw):
        if not hasattr(py.std, 'subprocess'):
            py.test.skip("no subprocess module")
        env = os.environ.copy()
        env['PYTHONPATH'] = ":".join(filter(None, [
            str(os.getcwd()), env.get('PYTHONPATH', '')]))
        kw['env'] = env
        #print "env", env
        return py.std.subprocess.Popen(cmdargs, stdout=stdout, stderr=stderr, **kw)

    def run(self, *cmdargs):
        return self._run(*cmdargs)

    def _run(self, *cmdargs):
        cmdargs = [str(x) for x in cmdargs]
        p1 = self.tmpdir.join("stdout")
        p2 = self.tmpdir.join("stderr")
        print_("running", cmdargs, "curdir=", py.path.local())
        f1 = p1.open("wb")
        f2 = p2.open("wb")
        now = time.time()
        popen = self.popen(cmdargs, stdout=f1, stderr=f2,
            close_fds=(sys.platform != "win32"))
        ret = popen.wait()
        f1.close()
        f2.close()
        out = p1.read("rb")
        out = getdecoded(out).splitlines()
        err = p2.read("rb")
        err = getdecoded(err).splitlines()
        def dump_lines(lines, fp):
            try:
                for line in lines:
                    py.builtin.print_(line, file=fp)
            except UnicodeEncodeError:
                print("couldn't print to %s because of encoding" % (fp,))
        dump_lines(out, sys.stdout)
        dump_lines(err, sys.stderr)
        return RunResult(ret, out, err, time.time()-now)

    def runpybin(self, scriptname, *args):
        fullargs = self._getpybinargs(scriptname) + args
        return self.run(*fullargs)

    def _getpybinargs(self, scriptname):
        if not self.request.config.getvalue("notoolsonpath"):
            script = py.path.local.sysfind(scriptname)
            assert script, "script %r not found" % scriptname
            return (script,)
        else:
            py.test.skip("cannot run %r with --no-tools-on-path" % scriptname)

    def runpython(self, script, prepend=True):
        if prepend:
            s = self._getsysprepend()
            if s:
                script.write(s + "\n" + script.read())
        return self.run(sys.executable, script)

    def _getsysprepend(self):
        if self.request.config.getvalue("notoolsonpath"):
            s = "import sys;sys.path.insert(0,%r);" % str(py._pydir.dirpath())
        else:
            s = ""
        return s

    def runpython_c(self, command):
        command = self._getsysprepend() + command
        return self.run(py.std.sys.executable, "-c", command)

    def runpytest(self, *args):
        p = py.path.local.make_numbered_dir(prefix="runpytest-",
            keep=None, rootdir=self.tmpdir)
        args = ('--basetemp=%s' % p, ) + args
        for x in args:
            if '--confcutdir' in str(x):
                break
        else:
            args = ('--confcutdir=.',) + args
        plugins = [x for x in self.plugins if isinstance(x, str)]
        if plugins:
            args = ('-p', plugins[0]) + args
        return self.runpybin("py.test", *args)

    def spawn_pytest(self, string, expect_timeout=10.0):
        if self.request.config.getvalue("notoolsonpath"):
            py.test.skip("--no-tools-on-path prevents running pexpect-spawn tests")
        basetemp = self.tmpdir.mkdir("pexpect")
        invoke = self._getpybinargs("py.test")[0]
        cmd = "%s --basetemp=%s %s" % (invoke, basetemp, string)
        return self.spawn(cmd, expect_timeout=expect_timeout)

    def spawn(self, cmd, expect_timeout=10.0):
        pexpect = py.test.importorskip("pexpect", "2.4")
        logfile = self.tmpdir.join("spawn.out")
        child = pexpect.spawn(cmd, logfile=logfile.open("w"))
        child.timeout = expect_timeout
        return child

def getdecoded(out):
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            return "INTERNAL not-utf8-decodeable, truncated string:\n%s" % (
                    py.io.saferepr(out),)

class PseudoPlugin:
    def __init__(self, vars):
        self.__dict__.update(vars)

class ReportRecorder(object):
    def __init__(self, hook):
        self.hook = hook
        self.pluginmanager = hook._pm
        self.pluginmanager.register(self)

    def getcall(self, name):
        return self.hookrecorder.getcall(name)

    def popcall(self, name):
        return self.hookrecorder.popcall(name)

    def getcalls(self, names):
        """ return list of ParsedCall instances matching the given eventname. """
        return self.hookrecorder.getcalls(names)

    # functionality for test reports

    def getreports(self, names="pytest_runtest_logreport pytest_collectreport"):
        return [x.report for x in self.getcalls(names)]

    def matchreport(self, inamepart="", names="pytest_runtest_logreport pytest_collectreport"):
        """ return a testreport whose dotted import path matches """
        l = []
        for rep in self.getreports(names=names):
            if not inamepart or inamepart in rep.nodenames:
                l.append(rep)
        if not l:
            raise ValueError("could not find test report matching %r: no test reports at all!" %
                (inamepart,))
        if len(l) > 1:
            raise ValueError("found more than one testreport matching %r: %s" %(
                             inamepart, l))
        return l[0]

    def getfailures(self, names='pytest_runtest_logreport pytest_collectreport'):
        return [rep for rep in self.getreports(names) if rep.failed]

    def getfailedcollections(self):
        return self.getfailures('pytest_collectreport')

    def listoutcomes(self):
        passed = []
        skipped = []
        failed = []
        for rep in self.getreports("pytest_runtest_logreport"):
            if rep.passed:
                if rep.when == "call":
                    passed.append(rep)
            elif rep.skipped:
                skipped.append(rep)
            elif rep.failed:
                failed.append(rep)
        return passed, skipped, failed

    def countoutcomes(self):
        return [len(x) for x in self.listoutcomes()]

    def assertoutcome(self, passed=0, skipped=0, failed=0):
        realpassed, realskipped, realfailed = self.listoutcomes()
        assert passed == len(realpassed)
        assert skipped == len(realskipped)
        assert failed == len(realfailed)

    def clear(self):
        self.hookrecorder.calls[:] = []

    def unregister(self):
        self.pluginmanager.unregister(self)
        self.hookrecorder.finish_recording()

class LineComp:
    def __init__(self):
        self.stringio = py.io.TextIO()

    def assert_contains_lines(self, lines2):
        """ assert that lines2 are contained (linearly) in lines1.
            return a list of extralines found.
        """
        __tracebackhide__ = True
        val = self.stringio.getvalue()
        self.stringio.truncate(0)
        self.stringio.seek(0)
        lines1 = val.split("\n")
        return LineMatcher(lines1).fnmatch_lines(lines2)

class LineMatcher:
    def __init__(self,  lines):
        self.lines = lines

    def str(self):
        return "\n".join(self.lines)

    def _getlines(self, lines2):
        if isinstance(lines2, str):
            lines2 = py.code.Source(lines2)
        if isinstance(lines2, py.code.Source):
            lines2 = lines2.strip().lines
        return lines2

    def fnmatch_lines_random(self, lines2):
        lines2 = self._getlines(lines2)
        for line in lines2:
            for x in self.lines:
                if line == x or fnmatch(x, line):
                    print_("matched: ", repr(line))
                    break
            else:
                raise ValueError("line %r not found in output" % line)

    def fnmatch_lines(self, lines2):
        lines2 = self._getlines(lines2)
        lines1 = self.lines[:]
        nextline = None
        extralines = []
        __tracebackhide__ = True
        for line in lines2:
            nomatchprinted = False
            while lines1:
                nextline = lines1.pop(0)
                if line == nextline:
                    print_("exact match:", repr(line))
                    break
                elif fnmatch(nextline, line):
                    print_("fnmatch:", repr(line))
                    print_("   with:", repr(nextline))
                    break
                else:
                    if not nomatchprinted:
                        print_("nomatch:", repr(line))
                        nomatchprinted = True
                    print_("    and:", repr(nextline))
                extralines.append(nextline)
            else:
                assert line == nextline
