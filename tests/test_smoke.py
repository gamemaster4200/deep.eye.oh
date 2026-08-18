import deep_eye_oh


def test_package_imports_and_has_version():
    assert isinstance(deep_eye_oh.__version__, str)
    assert deep_eye_oh.__version__
