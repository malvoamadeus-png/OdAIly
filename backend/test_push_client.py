from unittest.mock import Mock, patch

from packages.publisher.push_client import PushClient, content_to_paragraph_html


def test_content_to_paragraph_html_wraps_each_non_empty_line() -> None:
    assert content_to_paragraph_html("第一段。\n\n第二段包含 A&B < C。") == (
        "<p>第一段。</p><p>第二段包含 A&amp;B &lt; C。</p>"
    )


@patch("packages.publisher.push_client.requests.post")
def test_push_converts_content_only_when_building_request(mock_post: Mock) -> None:
    mock_post.return_value.raise_for_status.return_value = None
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "ok"
    client = PushClient(
        endpoint="https://example.test/push",
        timeout_seconds=1,
        max_attempts=1,
        backoff_seconds=0,
    )

    client.push(title="测试标题", content="第一段。\n第二段。", dry_run=False, is_publish=True)

    assert mock_post.call_args.kwargs["json"]["content"] == "<p>第一段。</p><p>第二段。</p>"
