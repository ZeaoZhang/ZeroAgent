"""Mocked end-to-end checks for generated file delivery across channel adapters."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _channel_module(name: str, dependency: str):
    pytest.importorskip(dependency)
    return importlib.import_module(f"zero_agent.bots.{name}")


@pytest.mark.asyncio
async def test_dingtalk_uploads_images_and_documents(tmp_path, monkeypatch):
    module = _channel_module("dingtalk_app", "dingtalk_stream")
    image = tmp_path / "diagram.png"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png")
    document.write_bytes(b"pdf")

    uploads = []
    sent = []

    class Response:
        def __init__(self, media_id):
            self.media_id = media_id

        def raise_for_status(self):
            pass

        def json(self):
            return {"media_id": self.media_id}

    def post(url, *, params, files, timeout):
        uploads.append((params["type"], files["media"][0], files["media"][1].read(), timeout))
        return Response(f"media-{len(uploads)}")

    app = object.__new__(module.DingTalkApp)
    app._get_access_token = AsyncMock(return_value="token")
    app._send_batch_message = AsyncMock(side_effect=lambda *args: sent.append(args) or True)
    monkeypatch.setattr(module.requests, "post", post)

    await app.send_file("chat", str(image))
    await app.send_file("chat", str(document))

    assert [(item[0], item[1], item[2]) for item in uploads] == [
        ("image", "diagram.png", b"png"),
        ("file", "report.pdf", b"pdf"),
    ]
    assert sent[0][1:] == ("sampleImageMsg", {"photoURL": "media-1"})
    assert sent[1][1:] == (
        "sampleFile",
        {"mediaId": "media-2", "fileName": "report.pdf", "fileType": "pdf"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_group", [False, True])
async def test_qq_uploads_images_and_documents_through_official_chunked_flow(
    tmp_path, monkeypatch, is_group,
):
    module = _channel_module("qq_app", "botpy")
    image = tmp_path / "diagram.png"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png-content")
    document.write_bytes(b"pdf-content")
    api_requests = []
    uploaded_parts = []
    messages = []

    class HTTP:
        async def request(self, route, *, json):
            api_requests.append((route.url, json))
            if route.url.endswith("/upload_prepare"):
                size = int(json["file_size"])
                block_size = 5
                return {
                    "upload_id": f"upload-{len(api_requests)}",
                    "block_size": block_size,
                    "parts": [
                        {"index": index, "presigned_url": f"https://upload.test/{index}"}
                        for index in range(1, (size + block_size - 1) // block_size + 1)
                    ],
                }
            if route.url.endswith("/upload_part_finish"):
                return {}
            if route.url.endswith("/files"):
                return {"file_info": f"uploaded-{len(messages) + 1}"}
            raise AssertionError(f"unexpected QQ API route: {route.url}")

    class API:
        _http = HTTP()
        async def post_c2c_message(self, **kwargs):
            messages.append(kwargs)
        async def post_group_message(self, **kwargs):
            messages.append(kwargs)

    class UploadResponse:
        def raise_for_status(self):
            pass

    def put(url, *, data, timeout):
        uploaded_parts.append((url, data, timeout))
        return UploadResponse()

    app = object.__new__(module.QQApp)
    app.client = SimpleNamespace(api=API())
    chat_id = "group-openid" if is_group else "user-openid"
    monkeypatch.setattr(module.requests, "put", put)
    await app.send_file(chat_id, str(image), msg_id="m1", is_group=is_group)
    await app.send_file(chat_id, str(document), msg_id="m2", is_group=is_group)

    endpoint_scope = "groups" if is_group else "users"
    assert all(
        f"/v2/{endpoint_scope}/{chat_id}/" in url for url, _ in api_requests
    )
    assert len([1 for url, _ in api_requests if url.endswith("/upload_prepare")]) == 2
    finish_payloads = [body for url, body in api_requests if url.endswith("/upload_part_finish")]
    assert len(finish_payloads) == 6
    assert [item["part_index"] for item in finish_payloads] == [1, 2, 3, 1, 2, 3]
    assert [item["block_size"] for item in finish_payloads] == ["5", "5", "1"] * 2
    prepare_payloads = [body for url, body in api_requests if url.endswith("/upload_prepare")]
    assert [item["file_type"] for item in prepare_payloads] == [1, 4]
    assert [item["file_name"] for item in prepare_payloads] == ["diagram.png", "report.pdf"]
    assert [item["md5"] for item in prepare_payloads] == [
        hashlib.md5(b"png-content").hexdigest(),
        hashlib.md5(b"pdf-content").hexdigest(),
    ]
    assert [item[1] for item in uploaded_parts] == [
        b"png-c", b"onten", b"t", b"pdf-c", b"onten", b"t",
    ]
    assert [item["msg_type"] for item in messages] == [7, 7]
    assert [item["msg_id"] for item in messages] == ["m1", "m2"]
    assert [item["media"] for item in messages] == [
        {"file_info": "uploaded-1"}, {"file_info": "uploaded-2"},
    ]
    assert all(item["group_openid" if is_group else "openid"] == chat_id for item in messages)


@pytest.mark.asyncio
async def test_qq_reports_invalid_upload_plan_instead_of_sending_empty_media(tmp_path):
    module = _channel_module("qq_app", "botpy")
    image = tmp_path / "diagram.png"
    image.write_bytes(b"png")
    messages = []

    class HTTP:
        async def request(self, route, *, json):
            return {"block_size": 1024, "parts": []}

    class API:
        _http = HTTP()
        async def post_c2c_message(self, **kwargs):
            messages.append(kwargs)

    app = object.__new__(module.QQApp)
    app.client = SimpleNamespace(api=API())
    app.send_text = AsyncMock()
    await app.send_file("openid", str(image), msg_id="m1")

    assert messages == []
    app.send_text.assert_awaited_once()
    assert "文件发送失败" in app.send_text.await_args.args[1]


@pytest.mark.asyncio
async def test_telegram_resolves_and_sends_images_and_other_files(tmp_path, monkeypatch):
    module = _channel_module("telegram_app", "telegram")
    image = tmp_path / "diagram.png"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png")
    document.write_bytes(b"pdf")

    class Reply:
        def __init__(self):
            self.photos = []
            self.documents = []
            self.notices = []

        async def reply_photo(self, fp):
            self.photos.append((Path(fp.name).name, fp.read()))

        async def reply_document(self, fp):
            self.documents.append((Path(fp.name).name, fp.read()))

        async def reply_text(self, text):
            self.notices.append(text)

    reply = Reply()
    monkeypatch.setattr(
        module, "runner", SimpleNamespace(config=SimpleNamespace(workspace_dir=str(tmp_path)))
    )
    assert module._files_from_text("Saved image [FILE:diagram.png] and file [FILE:report.pdf]") == [
        str(image), str(document),
    ]
    await module._send_files(reply, [str(image), str(document)])

    assert reply.photos == [("diagram.png", b"png")]
    assert reply.documents == [("report.pdf", b"pdf")]
    assert reply.notices == []


def test_wechat_sends_generated_document_as_file_item(tmp_path):
    module = _channel_module("wechat_app", "Crypto")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")
    calls = []
    client = object.__new__(module.WxBotClient)

    def post(endpoint, body):
        calls.append((endpoint, body))
        if endpoint.endswith("getuploadurl"):
            return {"upload_param": "upload-param"}
        return {"ret": 0}

    client._post = post
    client._upload = lambda filekey, param, raw, **kwargs: {
        "encrypt_query_param": "encrypted-upload",
    }
    client.send_file("user", str(document), context_token="context")

    assert [item[0] for item in calls] == ["ilink/bot/getuploadurl", "ilink/bot/sendmessage"]
    message = calls[-1][1]["msg"]
    assert message["context_token"] == "context"
    item = message["item_list"][0]
    assert item["type"] == module.ITEM_FILE
    assert item["file_item"]["file_name"] == "report.pdf"


@pytest.mark.asyncio
async def test_wecom_resolves_and_uploads_generated_file(tmp_path):
    module = _channel_module("wecom_app", "wecom_aibot_sdk")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")
    uploads = []
    sent = []

    class Client:
        async def upload_media(self, data, *, type, filename):
            uploads.append((data, type, filename))
            return {"media_id": "media-1"}

        async def send_media_message(self, chat_id, media_type, media_id):
            sent.append((chat_id, media_type, media_id))

    app = object.__new__(module.WeComApp)
    app.runner = SimpleNamespace(config=SimpleNamespace(workspace_dir=str(tmp_path)))
    app.client = Client()
    app.chat_frames = {}
    app.send_text = AsyncMock()
    await app.send_done("chat", "Saved as [FILE:report.pdf]")

    assert uploads == [(b"pdf", "file", "report.pdf")]
    assert sent == [("chat", "file", "media-1")]


def test_feishu_uploads_generated_images_and_documents(tmp_path, monkeypatch):
    module = _channel_module("feishu_app", "lark_oapi")
    image = tmp_path / "diagram.png"
    document = tmp_path / "report.pdf"
    image.write_bytes(b"png")
    document.write_bytes(b"pdf")
    uploaded_images = []
    uploaded_files = []
    sent = []
    monkeypatch.setattr(
        module, "runner", SimpleNamespace(config=SimpleNamespace(workspace_dir=str(tmp_path)))
    )
    monkeypatch.setattr(module, "_upload_image_sync", lambda path: uploaded_images.append(Path(path).name) or "img-key")
    monkeypatch.setattr(module, "_upload_file_sync", lambda path: uploaded_files.append(Path(path).name) or "file-key")
    monkeypatch.setattr(module, "send_message", lambda *args, **kwargs: sent.append((args, kwargs)))

    module._send_generated_files(
        "open-id", "Saved image [FILE:diagram.png] and document [FILE:report.pdf]", "open_id"
    )

    assert uploaded_images == ["diagram.png"]
    assert uploaded_files == ["report.pdf"]
    assert sorted(item[1]["msg_type"] for item in sent) == ["file", "image"]


@pytest.mark.asyncio
async def test_discord_sends_generated_file_as_attachment(tmp_path):
    module = _channel_module("discord_app", "discord")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")
    sent = []

    class Channel:
        async def send(self, content=None, *, file=None):
            sent.append((content, file))

    app = object.__new__(module.DiscordApp)
    app._channel_cache = {"ch:1": Channel()}
    app._get_runner = lambda _chat_id: SimpleNamespace(
        config=SimpleNamespace(workspace_dir=str(tmp_path))
    )
    await app.send_done("ch:1", "Report ready at [FILE:report.pdf]")

    assert sent[0][0].startswith("Report ready at")
    assert sent[1][0] is None
    assert sent[1][1].filename == "report.pdf"
    sent[1][1].close()


def test_streamlit_renders_and_offers_download_for_generated_files(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    module = importlib.import_module("zero_agent.frontends.stapp")
    image = tmp_path / "diagram.png"
    image.write_bytes(b"png")
    agent = SimpleNamespace(config=SimpleNamespace(workspace_dir=str(tmp_path)))
    files = module._output_files(agent, "Saved to [FILE:diagram.png]")
    rendered = []
    downloads = []
    monkeypatch.setattr(module.st, "image", lambda *args, **kwargs: rendered.append((args, kwargs)))
    monkeypatch.setattr(module.st, "download_button", lambda *args, **kwargs: downloads.append((args, kwargs)))

    module._render_output_files(files, "test")

    assert files == [str(image)]
    assert rendered and rendered[0][0][0] == b"png"
    assert downloads and downloads[0][1]["file_name"] == "diagram.png"
