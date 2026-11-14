"""User-facing texts (Chinese). Changing CONSENT_TEXT requires a new CONSENT_VERSION: consent_records keep
the version and the hash of the exact wording the user agreed to."""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from meeting_protocol import FailureCode

CONSENT_VERSION = "consent-v1"
CONSENT_TEXT = (
    "请确认以下事项后回复「同意」：\n"
    "1. 你是这场会议的参会者，或已获得会议组织者的许可；\n"
    "2. 助手将以「AI会议记录助手（代你记录）」的名义入会，并在会中公开说明；\n"
    "3. 主持人同意后才会开始录音，未获同意不录音；\n"
    "4. 录音仅用于生成会议纪要，默认保留 30 天，你可随时回复「删除」删除数据。"
)
CONSENT_TEXT_SHA256 = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()

# Fallback path (design doc §4 路径 D): the user supplies the recording or the text.
UPLOAD_CONSENT_VERSION = "upload-consent-v1"
UPLOAD_CONSENT_TEXT = (
    "请确认以下事项：\n"
    "1. 你是这场会议的参会者，或已获得会议组织者的许可，有权提供这段录音；\n"
    "2. 参会者已知道会议被录音，录音将用于生成会议纪要；\n"
    "3. 录音会被转写为文字，默认保留 30 天，纪要保留 180 天，你可随时回复「删除」删除数据。"
)
UPLOAD_CONSENT_SHA256 = hashlib.sha256(UPLOAD_CONSENT_TEXT.encode()).hexdigest()
FORWARDED_CONSENT_VERSION = "forwarded-consent-v1"
FORWARDED_CONSENT_TEXT = (
    "请确认：你是这场会议的参会者或已获组织者许可，有权提供这份会议内容；"
    "内容将用于生成结构化纪要，默认保留 180 天，你可随时回复「删除」删除数据。"
)
FORWARDED_CONSENT_SHA256 = hashlib.sha256(FORWARDED_CONSENT_TEXT.encode()).hexdigest()

TZ = ZoneInfo("Asia/Shanghai")


def _fmt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y/%m/%d %H:%M")


def confirm_meeting(meeting_code: str, starts_at: datetime, ends_at: datetime, subject: str | None) -> str:
    code = (
        f"{meeting_code[:3]}-{meeting_code[3:6]}-{meeting_code[6:]}"
        if len(meeting_code) == 9
        else meeting_code
    )
    title = f"「{subject}」" if subject else ""
    return f"收到会议{title}：会议号 {code}，时间 {_fmt(starts_at)}-{_fmt(ends_at)[-5:]}。\n\n{CONSENT_TEXT}"


ASK_TIME = "请告诉我会议时间（24 小时制），例如：2026/10/10 15:00-16:00"
ASK_PASSWORD = "这场会议有入会密码吗？有的话请直接回复密码；没有请回复「无密码」。"
NOT_UNDERSTOOD = (
    "请把腾讯会议的邀请信息（复制邀请）转发给我，我会帮你安排记录。"
    "会后也可以直接发来会议录音或逐字稿；录音较大时回复「上传录音」。"
)
MULTIPLE_MEETINGS = "一条消息里有多场会议，请一次只发一场会议的邀请。"
CODE_CONFLICT = "邀请里的会议号和链接或拨号信息不一致，请重新复制完整的会议邀请发给我。"
INVITE_ONLY = "这是一场「特邀会议」，只有被邀请的微信好友能加入，助手无法入会。"
REGISTRATION_REQUIRED = (
    "这场会议需要报名，助手无法替你报名入会。请联系主持人改为无需报名，或直接上传会议录音。"
)
NO_CAPACITY = "这个时间段助手已有其他会议，暂时无法安排。可以换个时间，或在会后上传录音。"
SCHEDULED = "已安排：助手会在会议开始前 3 分钟尝试入会。回复「取消」可随时取消。"
CANCELED = "已取消这场会议的记录任务；已录下的内容（如有）会被删除。"
NOTHING_TO_CANCEL = "当前没有可以取消的会议任务。"
STOPPED = "已停止录音，纪要生成后会通知你。"
MEETING_OVER = "这场会议已经结束，无法再安排记录。如需整理，可以在会后上传录音。"
NOTHING_TO_STOP = "当前没有正在录音的会议。"
RESULT_LINK_PLACEHOLDER = "{result_link}"  # replaced by a freshly minted one-time link when sending
COMPLETED = (
    f"会议纪要已生成：{RESULT_LINK_PLACEHOLDER}\n"
    "链接 24 小时内有效、只能打开一次；之后可随时回复「查看纪要」重新获取。"
)
RESULT_LINK = f"会议纪要：{RESULT_LINK_PLACEHOLDER}\n链接 24 小时内有效、只能打开一次。"
NO_RESULT = "目前没有可以查看的会议纪要。"

UPLOAD_LINK_PLACEHOLDER = "{upload_link}"  # replaced by a freshly minted one-time upload link when sending
MAX_UPLOAD_HINT = "微信里能直接发送的文件最大 20MB（视频 10MB）。"
UPLOAD_FORMATS = "支持 mp3、m4a、wav、mp4 等常见格式，最长 4 小时，最大 2GB。"
UPLOAD_LINK = (
    f"请用这个链接上传会议录音（24 小时内有效，只能上传一次）：{UPLOAD_LINK_PLACEHOLDER}\n" + UPLOAD_FORMATS
)
UPLOAD_CONFIRM = (
    "收到会议录音。\n\n" + UPLOAD_CONSENT_TEXT + "\n\n确认无误请回复「同意」，我会开始转写并生成纪要。"
)
FORWARDED_CONFIRM = (
    "收到会议内容（{chars} 字）。如果还有后续内容，请继续发送。\n\n"
    + FORWARDED_CONSENT_TEXT
    + "\n\n全部发完后请回复「同意」，我会生成结构化纪要。"
)
FORWARDED_FILE_CONFIRM = (
    "收到会议内容文件（{chars} 字）。\n\n"
    + FORWARDED_CONSENT_TEXT
    + "\n\n确认无误请回复「同意」，我会生成结构化纪要。"
)
MEETING_STILL_WAITING = "\n另有一场会议仍待确认，再回复「同意」即可安排。"
FORWARDED_APPENDED = "已收到后续内容，目前共 {chars} 字。全部发完后请回复「同意」。"
PROCESSING = "已开始处理，完成后会把纪要链接发给你。"
PROCESSING_MANY = "已开始处理你发来的 {count} 份材料，每份完成后会分别把纪要链接发给你。"
FORWARDED_DONE = "好的，内容已收齐。确认无误请回复「同意」。"
TEXT_TOO_LONG = "内容太长（超过约 9 万字），无法一次生成纪要。请分成几次、每次发一段会议的内容。"
LEGACY_DOC = "这是旧版 Word（.doc）文件，请另存为 .docx 或 txt 后再发给我。"
UNSUPPORTED_MESSAGE = (
    "暂不支持这种消息（图片、聊天记录等）。请发送会议邀请、会议录音、txt / Word 逐字稿，或直接粘贴文字。"
)
FILE_EXPIRED = "这个文件已过期（微信文件 3 天内有效），请重新发送。"
FILE_UNREADABLE = "这个文件暂时无法读取，请稍后重新发送；录音较大时请回复「上传录音」。"
VOICE_NOT_SUPPORTED = "语音消息太短，不能作为会议录音。请发送录音文件，或回复「上传录音」获取上传链接。"
PDF_NOT_SUPPORTED = "暂不支持 PDF。请从腾讯会议导出 txt 或 Word 格式的逐字稿后发给我，或直接把文字复制过来。"
UNSUPPORTED_FILE = (
    "无法识别这个文件。请发送会议录音（mp3、m4a、wav、mp4 等），或 txt / Word 格式的逐字稿。"
    + MAX_UPLOAD_HINT
    + "更大的录音请回复「上传录音」。"
)
SHARE_LINK_HINT = (
    "这是腾讯会议的云录制分享链接，需要分享人授权才能打开，我无法直接读取。"
    "请在腾讯会议里导出逐字稿（txt / Word）或音频后发给我；录音超过 20MB 请回复「上传录音」。"
)
TOO_LARGE_HINT = MAX_UPLOAD_HINT + "录音较大时请回复「上传录音」，我会发你一个上传链接。"
UPLOADING = "录音已结束，正在上传和整理。"

FAILURE_TEXT = {
    FailureCode.NO_WORKER: "到点时没有可用的记录助手，本次未能入会。",
    FailureCode.HOST_REJECTED: "主持人拒绝了助手入会。",
    FailureCode.MEETING_LOCKED: "会议已锁定，助手无法入会。",
    FailureCode.WRONG_PASSWORD: "入会密码不正确，请检查后重新发送邀请。",
    FailureCode.MEETING_NOT_STARTED_TIMEOUT: "会议迟迟没有开始，助手已放弃等待。",
    FailureCode.WAITING_ROOM_TIMEOUT: "助手在等候室等待超过 15 分钟未被放行。",
    FailureCode.MEETING_ENDED: "助手入会前会议已经结束。",
    FailureCode.REMOVED_BY_HOST: "助手被主持人移出会议，已录内容会被删除。",
    FailureCode.RECORDING_APPROVAL_TIMEOUT: "主持人未同意录音，助手已离会，没有录音。",
    FailureCode.RECORDING_DENIED: "主持人拒绝了录音，助手已离会，没有录音。",
    FailureCode.CONSENT_REVOKED: "录音许可被撤回，助手已停止录音，已录内容会被删除。",
    FailureCode.EMPTY_AUDIO: "录音中没有检测到有效声音，无法生成纪要。",
    FailureCode.UPLOAD_FAILED: "录音上传或获取失败，无法生成纪要；如果是你发来的文件，请重新发送。",
    FailureCode.ASR_FAILED: "语音转写失败，无法生成纪要。",
    FailureCode.LLM_FAILED: "纪要生成失败。",
    FailureCode.INVALID_UPLOAD: (
        "文件无法使用：需要是有声音的常见音视频格式（mp3、m4a、wav、mp4 等），最长 4 小时；"
        "文字内容需能识别且不超过约 9 万字。"
    ),
}


def failure(code: FailureCode | None) -> str:
    return "会议记录未能完成：" + FAILURE_TEXT.get(code, "发生内部错误。") if code else "会议记录未能完成。"


def message_failed(received_at: datetime) -> str:
    """Sent after the last attempt, maybe much later: names the message by its time, never its content."""
    return f"你在 {received_at.astimezone(TZ):%H:%M} 发来的消息没能处理成功，请重新发送一次。"
