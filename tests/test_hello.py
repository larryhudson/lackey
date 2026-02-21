"""Hello world test to verify pytest setup."""


def test_hello_world():
    """A simple hello world test."""
    assert True


def test_import_lackey():
    """Test that we can import the lackey module."""
    import lackey

    assert lackey is not None
