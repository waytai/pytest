class MonkeypatchPlugin:
    """ setattr-monkeypatching with automatical reversal after test. """
    def pytest_pyfuncarg_monkeypatch(self, pyfuncitem):
        monkeypatch = MonkeyPatch()
        pyfuncitem.addfinalizer(monkeypatch.finalize)
        return monkeypatch

notset = object()

class MonkeyPatch:
    def __init__(self):
        self._setattr = []
        self._setitem = []

    def setattr(self, obj, name, value):
        self._setattr.insert(0, (obj, name, getattr(obj, name, notset)))
        setattr(obj, name, value)

    def setitem(self, dictionary, name, value):
        self._setitem.insert(0, (dictionary, name, dictionary.get(name, notset)))
        dictionary[name] = value

    def finalize(self):
        for obj, name, value in self._setattr:
            if value is not notset:
                setattr(obj, name, value)
        for dictionary, name, value in self._setitem:
            if value is notset:
                del dictionary[name]
            else:
                dictionary[name] = value


def test_setattr():
    class A:
        x = 1
    monkeypatch = MonkeyPatch()
    monkeypatch.setattr(A, 'x', 2)
    assert A.x == 2
    monkeypatch.setattr(A, 'x', 3)
    assert A.x == 3
    monkeypatch.finalize()
    assert A.x == 1

def test_setitem():
    d = {'x': 1}
    monkeypatch = MonkeyPatch()
    monkeypatch.setitem(d, 'x', 2)
    monkeypatch.setitem(d, 'y', 1700)
    assert d['x'] == 2
    assert d['y'] == 1700
    monkeypatch.setitem(d, 'x', 3)
    assert d['x'] == 3
    monkeypatch.finalize()
    assert d['x'] == 1
    assert 'y' not in d

def test_monkeypatch_plugin(testdir):
    sorter = testdir.inline_runsource("""
        pytest_plugins = 'pytest_monkeypatch', 
        def test_method(monkeypatch):
            assert monkeypatch.__class__.__name__ == "MonkeyPatch"
    """)
    res = sorter.countoutcomes()
    assert tuple(res) == (1, 0, 0), res
        
