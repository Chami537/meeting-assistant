"""TC3-HMAC-SHA256 signing (official doc 213/30654 worked example) and the 录音文件识别 client."""

import hashlib
import json

import httpx
import pytest
from app.integrations.tencentcloud import api
from app.integrations.tencentcloud.asr import TencentAsr
from app.postprocess.providers import AsrState, ProviderError

# The official example: CVM DescribeInstances in ap-guangzhou at 1551113065. The doc masks the keys, so the
# key-independent steps are checked against its published hashes.
DOC_PAYLOAD = b'{"Limit": 1, "Filters": [{"Values": ["\\u672a\\u547d\\u540d"], "Name": "instance-name"}]}'


def test_canonical_request_matches_the_official_example():
    assert hashlib.sha256(DOC_PAYLOAD).hexdigest() == (
        "35e9c5b0e3ae67532d3c9f17ead6c90222632e5b1ff7f6e89887f1398934f064"
    )
    canonical = api.canonical_request("cvm.tencentcloudapi.com", "DescribeInstances", DOC_PAYLOAD)
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "7019a55be8395899b900fb5564e4200d984910f34794a27cb3fb7d10ff6a1e84"
    )


def test_authorization_header_shape_and_utc_date():
    header = api.authorization(
        secret_id="AKIDexample",
        secret_key="secret",
        service="cvm",
        host="cvm.tencentcloudapi.com",
        action="DescribeInstances",
        payload=DOC_PAYLOAD,
        timestamp=1551113065,  # 2019-02-26 in UTC+8, 2019-02-25 in UTC
    )
    prefix = (
        "TC3-HMAC-SHA256 Credential=AKIDexample/2019-02-25/cvm/tc3_request, "
        "SignedHeaders=content-type;host;x-tc-action, Signature="
    )
    assert header.startswith(prefix)
    assert len(header.removeprefix(prefix)) == 64
    other = api.authorization(
        secret_id="AKIDexample",
        secret_key="other",
        service="cvm",
        host="cvm.tencentcloudapi.com",
        action="DescribeInstances",
        payload=DOC_PAYLOAD,
        timestamp=1551113065,
    )
    assert other != header


class FakeAsrApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        return httpx.Response(200, json={"Response": {**self.responses.pop(0), "RequestId": "r"}})


def asr_client(*responses):
    fake = FakeAsrApi(responses)
    http = httpx.AsyncClient(
        base_url="https://asr.tencentcloudapi.com", transport=httpx.MockTransport(fake.handler)
    )
    cloud = api.TencentCloudApi(
        "AKID", "key", service="asr", version="2019-06-14", http=http, clock=lambda: 1.7e9
    )
    return TencentAsr(cloud), fake


async def test_submit_sends_a_signed_url_task():
    client, fake = asr_client({"Data": {"TaskId": 1000000286}})
    assert await client.submit("https://cos.example/a.m4a?sig=1") == "1000000286"
    [req] = fake.requests
    assert req.headers["X-TC-Action"] == "CreateRecTask"
    assert req.headers["X-TC-Version"] == "2019-06-14"
    assert req.headers["X-TC-Timestamp"] == "1700000000"
    assert req.headers["Authorization"].startswith(
        "TC3-HMAC-SHA256 Credential=AKID/2023-11-14/asr/tc3_request"
    )
    body = json.loads(req.content)
    assert body == {
        "EngineModelType": "16k_zh_en_meeting",
        "ChannelNum": 1,
        "ResTextFormat": 2,
        "SourceType": 0,
        "Url": "https://cos.example/a.m4a?sig=1",
        "SpeakerDiarization": 1,
        "SpeakerNumber": 0,
    }
    # the signature covers exactly the bytes sent
    expected = api.authorization(
        secret_id="AKID",
        secret_key="key",
        service="asr",
        host="asr.tencentcloudapi.com",
        action="CreateRecTask",
        payload=req.content,
        timestamp=1700000000,
    )
    assert req.headers["Authorization"] == expected


@pytest.mark.parametrize("status", [0, 1])
async def test_poll_running(status):
    client, fake = asr_client({"Data": {"TaskId": 5, "Status": status, "StatusStr": "doing"}})
    assert (await client.poll("5")).state == AsrState.RUNNING
    assert json.loads(fake.requests[0].content) == {"TaskId": 5}


async def test_poll_done_reads_sentences():
    detail = [
        {"FinalSentence": "大家好。", "StartMs": 20, "EndMs": 2380, "SpeakerId": 0},
        {"FinalSentence": "开始吧。", "StartMs": 2500, "EndMs": 3100, "SpeakerId": 1},
    ]
    client, _ = asr_client({"Data": {"Status": 2, "ResultDetail": detail, "AudioDuration": 3.1}})
    result = await client.poll("5")
    assert result.state == AsrState.DONE
    assert [(s.start_ms, s.end_ms, s.speaker, s.text) for s in result.segments] == [
        (20, 2380, "0", "大家好。"),
        (2500, 3100, "1", "开始吧。"),
    ]
    assert result.billed_seconds == 4


async def test_poll_done_falls_back_to_result_lines():
    text = "[0:0.020,0:2.380,0]  腾讯云语音识别欢迎您。\n[1:02:3.500,1:02:4.000]  再见。\n"
    client, _ = asr_client({"Data": {"Status": 2, "Result": text, "AudioDuration": 0}})
    result = await client.poll("5")
    assert [(s.start_ms, s.end_ms, s.speaker, s.text) for s in result.segments] == [
        (20, 2380, "0", "腾讯云语音识别欢迎您。"),
        (3_723_500, 3_724_000, None, "再见。"),
    ]
    assert result.billed_seconds is None


async def test_poll_failed_and_unknown_task():
    client, _ = asr_client(
        {"Data": {"Status": 3, "ErrorMsg": "Failed to download audio file!"}},
        {"Error": {"Code": "FailedOperation.NoSuchTask", "Message": "错误的TaskId"}},
    )
    failed = await client.poll("5")
    assert (failed.state, failed.error) == (AsrState.FAILED, "Failed to download audio file!")
    gone = await client.poll("5")
    assert (gone.state, gone.error) == (AsrState.FAILED, "FailedOperation.NoSuchTask")


async def test_api_errors_keep_only_the_code():
    client, _ = asr_client(
        {"Error": {"Code": "InvalidParameterValue", "Message": "bad url https://cos.example/a?sig=SECRET"}}
    )
    with pytest.raises(ProviderError) as exc:
        await client.submit("https://cos.example/a?sig=SECRET")
    assert str(exc.value) == "InvalidParameterValue"
