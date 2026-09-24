import httpx
import pytest
import respx

from editorial_bot.services.gitlab import GitLab, neutralize_quick_actions

BASE = "https://gitlab.com/api/v4/projects/sitcon-tw%2Feditorial%2Fboard"


@pytest.fixture
async def gitlab():
    api = GitLab("https://gitlab.com", "sitcon-tw/editorial/board", "test-token")
    yield api
    await api.close()


@respx.mock
async def test_unknown_label_never_reaches_create(gitlab):
    respx.get(BASE + "/labels").mock(return_value=httpx.Response(200, json=[{"name": "社群文案"}]))
    create = respx.post(BASE + "/issues").mock(return_value=httpx.Response(201, json={}))
    with pytest.raises(ValueError, match="不會建立"):
        await gitlab.create({"title": "test", "labels": ["invented"]})
    assert not create.called


@respx.mock
async def test_status_preserves_unrelated_labels_and_review_is_open(gitlab):
    labels = ["Status::Doing", "Status::Review", "SITCON::2027", "社群文案"]
    respx.get(BASE + "/labels").mock(return_value=httpx.Response(200, json=[{"name": s} for s in labels]))
    original = {"iid": 1, "labels": ["Status::Doing", "SITCON::2027", "社群文案"]}
    updated = {**original, "labels": ["Status::Review", "SITCON::2027", "社群文案"]}
    respx.get(BASE + "/issues/1").mock(
        side_effect=[httpx.Response(200, json=original), httpx.Response(200, json=updated)]
    )
    put = respx.put(BASE + "/issues/1").mock(return_value=httpx.Response(200, json=updated))
    actual = await gitlab.update(1, add_labels=["Status::Review"])
    import json

    payload = json.loads(put.calls[0].request.content)
    assert payload == {"add_labels": "Status::Review", "remove_labels": "Status::Doing"}
    assert actual["labels"] == updated["labels"]
    respx.get(BASE + "/issues").mock(return_value=httpx.Response(200, json=[updated]))
    assert await gitlab.search() == [updated]


def test_quick_actions_cannot_bypass_label_whitelist():
    text = "內容\n/label imaginary\n /close"
    assert neutralize_quick_actions(text) == "內容\n\\/label imaginary\n \\/close"


@respx.mock
async def test_mutating_timeout_is_not_blindly_retried(gitlab):
    route = respx.post(BASE + "/issues/1/notes").mock(side_effect=httpx.ReadTimeout("timeout"))
    from editorial_bot.services.gitlab import RemoteError

    with pytest.raises(RemoteError) as error:
        await gitlab.comment(1, "review")
    assert error.value.uncertain
    assert route.call_count == 1


@respx.mock
async def test_gitlab_username_is_resolved_by_exact_id(gitlab):
    respx.get("https://gitlab.com/api/v4/users/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "username": "gitlab.writer-1"})
    )
    assert await gitlab.user_username(42) == "gitlab.writer-1"


@respx.mock
async def test_missing_gitlab_account_allows_explicit_fallback(gitlab):
    respx.get("https://gitlab.com/api/v4/users/42").mock(return_value=httpx.Response(404))
    assert await gitlab.user_username(42) is None


@pytest.mark.parametrize("status", [403, 429, 503])
@respx.mock
async def test_failed_gitlab_lookup_is_not_a_missing_account(gitlab, status):
    from editorial_bot.services.gitlab import RemoteError

    respx.get("https://gitlab.com/api/v4/users/42").mock(return_value=httpx.Response(status))
    with pytest.raises(RemoteError):
        await gitlab.user_username(42)


@pytest.mark.parametrize("data", [{"id": 43, "username": "someone_else"}, {"id": 42, "username": "bad\n/close"}])
@respx.mock
async def test_invalid_gitlab_identity_response_is_rejected(gitlab, data):
    respx.get("https://gitlab.com/api/v4/users/42").mock(return_value=httpx.Response(200, json=data))
    with pytest.raises(ValueError):
        await gitlab.user_username(42)
