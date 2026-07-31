from app.urls import project_url


def test_project_url_encodes_exactly_one_path_component() -> None:
    assert project_url("a" * 32) == f"/projects/{'a' * 32}"
    assert project_url("50% #?&+ off", "/chat") == (
        "/projects/50%25%20%23%3F%26%2B%20off/chat"
    )


def test_project_url_rejects_a_non_path_suffix() -> None:
    try:
        project_url("Project", "chat")
    except ValueError as exc:
        assert str(exc) == "project URL suffix must start with '/' or '?'"
    else:
        raise AssertionError("non-path suffix was accepted")
