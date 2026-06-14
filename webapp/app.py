import sys
import re
import os
import math
import tempfile
import subprocess
from collections import Counter

# Dùng Python hiện tại để gọi yt-dlp (tránh lỗi PATH trên Windows)
PYTHON = sys.executable
YTDLP = [PYTHON, "-m", "yt_dlp"]

# Đường dẫn ffmpeg: Windows dùng winget path, Linux/Render dùng system ffmpeg
_WIN_FFMPEG = r"C:\Users\ADMIN\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin"
FFMPEG_DIR  = _WIN_FFMPEG if sys.platform == "win32" else ""
FFMPEG_ARGS = ["--ffmpeg-location", FFMPEG_DIR] if FFMPEG_DIR else []

sys.stdout.reconfigure(encoding="utf-8")

from flask import Flask, request, jsonify, send_from_directory
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

app = Flask(__name__, static_folder="static")

# ─────────────────────────────────────────
# NHẬN DIỆN NỀN TẢNG
# ─────────────────────────────────────────
def nhan_dien_nen_tang(link):
    if "youtube.com" in link or "youtu.be" in link:
        return "youtube"
    if "tiktok.com" in link:
        return "tiktok"
    if "facebook.com" in link or "fb.com" in link or "fb.watch" in link:
        return "facebook"
    return None


def lay_id_youtube(link):
    # Hỗ trợ cả /watch?v=, /shorts/, youtu.be/
    mau = r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})"
    ket_qua = re.search(mau, link)
    return ket_qua.group(1) if ket_qua else None


# ─────────────────────────────────────────
# PHƯƠNG THỨC 1: YouTube (API phụ đề)
# ─────────────────────────────────────────
def lay_youtube(link):
    video_id = lay_id_youtube(link)
    if not video_id:
        raise ValueError("Không tìm được ID video YouTube.")

    api = YouTubeTranscriptApi()
    ds = api.list(video_id)
    try:
        phu_de = ds.find_transcript(["vi", "en"])
    except Exception:
        phu_de = next(iter(ds))

    snippets = api.fetch(phu_de.video_id, languages=[phu_de.language_code])
    toan_bo = " ".join(s.text.replace("\n", " ").strip() for s in snippets if s.text.strip())
    return re.sub(r" +", " ", toan_bo), phu_de.language_code


# ─────────────────────────────────────────
# PHƯƠNG THỨC 2: yt-dlp (TikTok / Facebook)
# Bước 1: thử lấy phụ đề có sẵn
# Bước 2: nếu không có → tải audio → Whisper
# ─────────────────────────────────────────
def lay_phu_de_ytdlp(link, thu_muc):
    """Thử tải phụ đề tự động bằng yt-dlp. Trả về nội dung hoặc None."""
    lenh = YTDLP + [
        "--write-auto-subs",
        "--sub-format", "vtt",
        "--skip-download",
        "--impersonate", "chrome",
        *FFMPEG_ARGS,
        "--output", os.path.join(thu_muc, "sub"),
        link,
    ]
    try:
        subprocess.run(lenh, capture_output=True, timeout=60)
        for f in os.listdir(thu_muc):
            if f.endswith(".vtt"):
                with open(os.path.join(thu_muc, f), encoding="utf-8") as fh:
                    return doc_vtt(fh.read())
    except Exception:
        pass
    return None


def doc_vtt(noi_dung):
    """Chuyển file VTT thành văn bản thuần."""
    dong = noi_dung.splitlines()
    cac_text = []
    for d in dong:
        d = d.strip()
        if not d or d.startswith("WEBVTT") or "-->" in d or re.match(r"^\d+$", d):
            continue
        # Xóa thẻ HTML như <c>, </c>
        d = re.sub(r"<[^>]+>", "", d)
        if d and (not cac_text or d != cac_text[-1]):
            cac_text.append(d)
    return " ".join(cac_text)


def lay_audio_va_phan_tich(link, thu_muc):
    """Tải audio bằng yt-dlp rồi dùng Whisper để nhận dạng giọng nói."""
    duong_dan_audio = os.path.join(thu_muc, "audio.mp3")

    # Tải audio
    lenh_tai = YTDLP + [
        "-x", "--audio-format", "mp3",
        "--impersonate", "chrome",
        *FFMPEG_ARGS,
        "--output", duong_dan_audio,
        link,
    ]
    ket_qua = subprocess.run(lenh_tai, capture_output=True, timeout=120)
    if ket_qua.returncode != 0:
        raise ValueError(f"Không tải được audio: {ket_qua.stderr.decode()[:200]}")

    # Tìm file audio thực tế (yt-dlp có thể thêm đuôi)
    file_audio = duong_dan_audio
    if not os.path.exists(file_audio):
        for f in os.listdir(thu_muc):
            if f.startswith("audio"):
                file_audio = os.path.join(thu_muc, f)
                break

    # Whisper nhận dạng giọng nói (model tiny ~39MB, tải 1 lần)
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(file_audio, beam_size=5)

    van_ban = " ".join(seg.text.strip() for seg in segments)
    return van_ban, info.language


def lay_khac(link):
    """Lấy lời thoại từ TikTok / Facebook."""
    with tempfile.TemporaryDirectory() as tmp:
        # Thử lấy phụ đề trước
        van_ban = lay_phu_de_ytdlp(link, tmp)
        if van_ban and len(van_ban) > 50:
            return van_ban, "auto"
        # Không có phụ đề → dùng Whisper
        return lay_audio_va_phan_tich(link, tmp)


# ─────────────────────────────────────────
# ĐỊNH DẠNG VĂN BẢN
# ─────────────────────────────────────────
def dinh_dang_doan(toan_bo, cau_moi_doan=5):
    cac_cau = re.split(r'(?<=[.!?])\s+', toan_bo.strip())
    cac_cau = [c.strip() for c in cac_cau if c.strip()]
    cac_doan = []
    for i in range(0, len(cac_cau), cau_moi_doan):
        cac_doan.append(" ".join(cac_cau[i:i + cau_moi_doan]))
    return "\n\n".join(cac_doan) if cac_doan else toan_bo


# ─────────────────────────────────────────
# PHÂN TÍCH VIRAL
# ─────────────────────────────────────────

# Từ điển cảm xúc tiếng Việt
TU_CAM_XUC = {
    "tich_cuc": ["tuyệt","hay","tốt","đỉnh","xuất sắc","tuyệt vời","hạnh phúc","vui","thích",
                 "yêu","cảm ơn","great","amazing","love","best","wow","incredible","fantastic"],
    "tieu_cuc": ["sợ","kinh","ghê","tệ","tồi","xấu","đau","khóc","thất bại","mất","nghèo",
                 "bad","fear","scared","terrible","worst","awful","hate","sad"],
    "bat_ngo":  ["sốc","không ngờ","bất ngờ","thật ra","sự thật","tiết lộ","bí mật","cảnh báo",
                 "shock","unbelievable","secret","truth","reveal","warning","nobody knows"],
    "tay_mo":   ["tại sao","làm thế nào","bí quyết","cách","mẹo","hacks","tip","how to",
                 "why","trick","method","strategy","formula","secret"],
    "khan_cap": ["ngay","liền","gấp","khẩn","hạn chót","cuối cùng","lần đầu","lần cuối",
                 "now","urgent","immediately","limited","deadline","first time","last chance"],
}

TU_CTA = ["like","share","follow","đăng ký","subscribe","comment","bình luận","chia sẻ",
           "theo dõi","lưu","save","tag","mention","repost","turn on","thông báo"]

TU_CAU_HOI = ["?","tại sao","làm sao","như thế nào","có phải","bạn có","bạn đã","ai","gì","khi nào",
              "why","how","what","who","when","where","did you","have you","do you"]

TU_KE_CHUYEN = ["câu chuyện","hồi đó","lúc đó","khi tôi","ngày xưa","bắt đầu","kết thúc",
                "hành trình","trải nghiệm","story","when i","back then","journey","experience",
                "i remember","it started","the moment"]

TU_GIA_TRI = ["dạy","học","hướng dẫn","cách","bước","tip","mẹo","bí quyết","công thức",
              "teach","learn","guide","step","how to","tutorial","lesson","advice","trick"]

TU_DONG_CAM = ["bạn","mình","chúng ta","ai cũng","mọi người","nhiều người","cảm giác","hiểu",
               "you","we","everyone","most people","you know","feel","understand","relate"]


def dem_tu_nhom(van_ban_lower, nhom):
    return sum(1 for w in nhom if w in van_ban_lower)


def phan_tich_hook(van_ban):
    """Phân tích 10% đầu video — phần quyết định giữ người xem."""
    do_dai = len(van_ban)
    hook = van_ban[:max(do_dai // 10, 200)]
    hook_lower = hook.lower()

    diem = 0
    nhan_xet = []

    # Có câu hỏi không?
    so_cau_hoi = dem_tu_nhom(hook_lower, TU_CAU_HOI)
    if so_cau_hoi >= 2:
        diem += 30
        nhan_xet.append("✅ Hook mở đầu bằng câu hỏi kích thích sự tò mò")
    elif so_cau_hoi == 1:
        diem += 15
        nhan_xet.append("☑️ Hook có câu hỏi nhưng chưa đủ mạnh")

    # Có từ bất ngờ/tiết lộ không?
    so_bat_ngo = dem_tu_nhom(hook_lower, TU_CAM_XUC["bat_ngo"])
    if so_bat_ngo >= 1:
        diem += 25
        nhan_xet.append("✅ Hook chứa yếu tố bất ngờ/tiết lộ bí mật — giữ người xem hiệu quả")

    # Có hứa hẹn giá trị không?
    so_gia_tri = dem_tu_nhom(hook_lower, TU_GIA_TRI)
    if so_gia_tri >= 1:
        diem += 20
        nhan_xet.append("✅ Hook hứa hẹn giá trị cụ thể cho người xem")

    # Có tạo khẩn cấp không?
    if dem_tu_nhom(hook_lower, TU_CAM_XUC["khan_cap"]) >= 1:
        diem += 15
        nhan_xet.append("✅ Hook tạo cảm giác khẩn cấp")

    if diem == 0:
        nhan_xet.append("⚠️ Hook chưa mạnh — mở đầu chưa tạo được sự tò mò ngay lập tức")

    return min(diem, 100), nhan_xet, hook[:150] + "..."


def phan_tich_cam_xuc(van_ban):
    """Đo cường độ cảm xúc trong toàn bộ nội dung."""
    vb = van_ban.lower()
    so_tu = len(re.findall(r'\w+', vb))
    if so_tu == 0:
        return 0, []

    ket_qua = {}
    for loai, tu_list in TU_CAM_XUC.items():
        so_lan = dem_tu_nhom(vb, tu_list)
        ket_qua[loai] = so_lan

    tong = sum(ket_qua.values())
    mat_do = tong / so_tu * 100  # % từ cảm xúc

    diem = min(int(mat_do * 400), 100)
    nhan_xet = []

    if ket_qua["bat_ngo"] >= 3:
        nhan_xet.append(f"✅ Yếu tố bất ngờ/tiết lộ xuất hiện {ket_qua['bat_ngo']} lần — kích thích chia sẻ mạnh")
    if ket_qua["tay_mo"] >= 3:
        nhan_xet.append(f"✅ Có {ket_qua['tay_mo']} từ kích thích tò mò — người xem muốn biết thêm")
    if ket_qua["tich_cuc"] > ket_qua["tieu_cuc"] * 2:
        nhan_xet.append("✅ Cảm xúc tích cực chiếm ưu thế — tạo cảm giác tốt cho người xem")
    elif ket_qua["tieu_cuc"] >= 3:
        nhan_xet.append(f"⚡ Video khai thác nỗi đau/nỗi sợ ({ket_qua['tieu_cuc']} lần) — chiến lược viral hiệu quả nhưng rủi ro")
    if ket_qua["khan_cap"] >= 2:
        nhan_xet.append(f"✅ Tạo cảm giác khẩn cấp {ket_qua['khan_cap']} lần — thúc đẩy hành động ngay")

    if not nhan_xet:
        nhan_xet.append("⚠️ Cảm xúc trong video còn nhạt — thiếu yếu tố kích thích chia sẻ")

    return diem, nhan_xet


def phan_tich_gia_tri(van_ban):
    """Đánh giá mức độ giá trị thực tế video mang lại."""
    vb = van_ban.lower()
    so_tu = len(re.findall(r'\w+', vb))

    so_gia_tri = dem_tu_nhom(vb, TU_GIA_TRI)
    so_dong_cam = dem_tu_nhom(vb, TU_DONG_CAM)
    so_cau_hoi = dem_tu_nhom(vb, TU_CAU_HOI)

    diem = min(so_gia_tri * 12 + so_dong_cam * 8 + so_cau_hoi * 5, 100)
    nhan_xet = []

    if so_gia_tri >= 5:
        nhan_xet.append(f"✅ Nội dung giàu giá trị thực tiễn ({so_gia_tri} lần đề cập hướng dẫn/mẹo)")
    elif so_gia_tri >= 2:
        nhan_xet.append(f"☑️ Có đề cập giá trị ({so_gia_tri} lần) nhưng có thể cụ thể hơn")
    else:
        nhan_xet.append("⚠️ Thiếu giá trị thực tiễn — người xem không biết họ được gì sau khi xem")

    if so_dong_cam >= 5:
        nhan_xet.append(f"✅ Tính đồng cảm cao ({so_dong_cam} lần dùng 'bạn/mình/chúng ta') — người xem cảm thấy được nói đến")
    elif so_dong_cam < 2:
        nhan_xet.append("⚠️ Thiếu tính đồng cảm — video nói về 'tôi' thay vì hỏi 'bạn cần gì'")

    return diem, nhan_xet


def phan_tich_cta(van_ban):
    """Đánh giá lời kêu gọi hành động."""
    vb = van_ban.lower()
    so_cta = dem_tu_nhom(vb, TU_CTA)
    diem = min(so_cta * 20, 100)
    nhan_xet = []

    if so_cta >= 3:
        nhan_xet.append(f"✅ CTA mạnh — kêu gọi hành động {so_cta} lần xuyên suốt video")
    elif so_cta >= 1:
        nhan_xet.append(f"☑️ Có {so_cta} CTA nhưng nên nhắc lại nhiều hơn (tối thiểu 3 lần)")
    else:
        nhan_xet.append("❌ Không có CTA — video thiếu lời kêu gọi like/share/follow, ảnh hưởng lớn đến viral")

    return diem, nhan_xet


def phan_tich_ke_chuyen(van_ban):
    """Đánh giá cấu trúc kể chuyện (storytelling)."""
    vb = van_ban.lower()
    so_ke_chuyen = dem_tu_nhom(vb, TU_KE_CHUYEN)

    # Kiểm tra cấu trúc: đầu / thân / cuối
    do_dai = len(van_ban)
    co_dau = any(w in van_ban[:do_dai//5].lower() for w in ["bắt đầu","mở đầu","đầu tiên","start","begin","first"])
    co_cuoi = any(w in van_ban[do_dai*4//5:].lower() for w in ["kết luận","cuối cùng","tóm lại","conclusion","finally","in the end"])

    diem = min(so_ke_chuyen * 15 + (20 if co_dau else 0) + (20 if co_cuoi else 0), 100)
    nhan_xet = []

    if so_ke_chuyen >= 3:
        nhan_xet.append(f"✅ Có yếu tố kể chuyện rõ ràng ({so_ke_chuyen} lần) — giúp người xem kết nối cảm xúc")
    if co_dau and co_cuoi:
        nhan_xet.append("✅ Cấu trúc mở - thân - kết rõ ràng — giữ người xem đến hết video")
    elif not co_dau and not co_cuoi:
        nhan_xet.append("⚠️ Thiếu cấu trúc câu chuyện — nội dung có thể bị rời rạc, khó theo dõi")

    if diem == 0:
        nhan_xet.append("⚠️ Không có yếu tố kể chuyện — video dạng liệt kê thông tin đơn thuần")

    return diem, nhan_xet


def tao_bao_cao_viral(van_ban, nen_tang="youtube"):
    """Tổng hợp toàn bộ phân tích viral thành báo cáo."""
    vb_raw = van_ban.replace("\n\n", " ").replace("\n", " ")

    diem_hook,     nx_hook,     hook_text = phan_tich_hook(vb_raw)
    diem_cam_xuc,  nx_cam_xuc             = phan_tich_cam_xuc(vb_raw)
    diem_gia_tri,  nx_gia_tri             = phan_tich_gia_tri(vb_raw)
    diem_cta,      nx_cta                 = phan_tich_cta(vb_raw)
    diem_ke_chuyen,nx_ke_chuyen           = phan_tich_ke_chuyen(vb_raw)

    # Trọng số: Hook 30%, Cảm xúc 25%, Giá trị 20%, CTA 15%, Kể chuyện 10%
    diem_tong = int(
        diem_hook * 0.30 +
        diem_cam_xuc * 0.25 +
        diem_gia_tri * 0.20 +
        diem_cta * 0.15 +
        diem_ke_chuyen * 0.10
    )

    if diem_tong >= 75:
        nhan_dinh = "🔥 Tiềm năng viral rất cao"
        mo_ta = "Video có đủ các yếu tố để lan truyền mạnh. Nếu được đẩy đúng thời điểm và đúng audience, khả năng viral là rất lớn."
    elif diem_tong >= 50:
        nhan_dinh = "⚡ Tiềm năng viral trung bình"
        mo_ta = "Video có một số yếu tố tốt nhưng chưa đủ để bùng phát. Cần cải thiện những điểm yếu bên dưới để tăng khả năng lan truyền."
    elif diem_tong >= 25:
        nhan_dinh = "📈 Tiềm năng viral thấp"
        mo_ta = "Video thiếu nhiều yếu tố viral quan trọng. Cần xem lại từ hook, cảm xúc đến CTA để cải thiện."
    else:
        nhan_dinh = "😴 Rất khó viral"
        mo_ta = "Video hiện tại chưa có yếu tố kích thích chia sẻ. Cần làm lại từ đầu với chiến lược rõ ràng hơn."

    return {
        "diem_tong": diem_tong,
        "nhan_dinh": nhan_dinh,
        "mo_ta": mo_ta,
        "hook_text": hook_text,
        "cac_yeu_to": [
            {
                "ten": "🎣 Hook (10 giây đầu)",
                "diem": diem_hook,
                "trong_so": "30%",
                "nhan_xet": nx_hook
            },
            {
                "ten": "❤️ Cảm xúc & Kích thích",
                "diem": diem_cam_xuc,
                "trong_so": "25%",
                "nhan_xet": nx_cam_xuc
            },
            {
                "ten": "💎 Giá trị & Đồng cảm",
                "diem": diem_gia_tri,
                "trong_so": "20%",
                "nhan_xet": nx_gia_tri
            },
            {
                "ten": "📣 Lời kêu gọi (CTA)",
                "diem": diem_cta,
                "trong_so": "15%",
                "nhan_xet": nx_cta
            },
            {
                "ten": "📖 Kể chuyện (Storytelling)",
                "diem": diem_ke_chuyen,
                "trong_so": "10%",
                "nhan_xet": nx_ke_chuyen
            },
        ]
    }


# ─────────────────────────────────────────
# TÓM TẮT (chạy offline, không cần API)
# ─────────────────────────────────────────
def tach_cau(van_ban):
    cac_cau = re.split(r'(?<=[.!?])\s+', van_ban.strip())
    return [c.strip() for c in cac_cau if len(c.strip()) > 20]


def tinh_diem_cau(cac_cau):
    stop_words = {
        "là","và","của","có","được","cho","với","trong","này","đó","một",
        "các","những","thì","mà","để","đã","sẽ","không","về","hay","cũng",
        "như","từ","khi","nên","vì","theo","bởi","lại","ra","vào","lên",
        "đến","còn","rồi","nếu","nhưng","tuy","hoặc","tôi","bạn","anh",
        "chị","mình","họ","nó","ta","chúng","thôi","thật","rất","quá",
    }
    tat_ca_tu = []
    for cau in cac_cau:
        tu = [w.lower() for w in re.findall(r'\w+', cau) if w.lower() not in stop_words and len(w) > 1]
        tat_ca_tu.extend(tu)
    tan_suat = Counter(tat_ca_tu)
    max_ts = max(tan_suat.values()) if tan_suat else 1
    diem_tu = {t: c / max_ts for t, c in tan_suat.items()}
    diem_cau = []
    for i, cau in enumerate(cac_cau):
        tu = [w.lower() for w in re.findall(r'\w+', cau)]
        d = sum(diem_tu.get(w, 0) for w in tu)
        diem_cau.append((i, cau, d / math.sqrt(len(tu)) if tu else 0))
    return diem_cau


def tao_dan_y(van_ban):
    cac_cau = tach_cau(van_ban)
    if not cac_cau:
        return "Không đủ nội dung để tóm tắt."
    tong = len(cac_cau)
    diem_cau = tinh_diem_cau(cac_cau)
    ps = max(tong // 5, 3)
    cac_phan = [
        ("Giới thiệu & bối cảnh",   cac_cau[:ps]),
        ("Nội dung chính — phần 1", cac_cau[ps:ps*2]),
        ("Nội dung chính — phần 2", cac_cau[ps*2:ps*3]),
        ("Nội dung chính — phần 3", cac_cau[ps*3:ps*4]),
        ("Kết luận & thông điệp",   cac_cau[ps*4:]),
    ]
    top5 = sorted(sorted(diem_cau, key=lambda x: x[2], reverse=True)[:5], key=lambda x: x[0])
    ket_qua = ["## 🎯 Chủ đề chính"]
    dau = " ".join(cac_cau[:2])
    ket_qua.append(dau[:300] + ("..." if len(dau) > 300 else ""))
    ket_qua += ["", "## 📋 Dàn ý chi tiết", ""]
    so_la_ma = ["I", "II", "III", "IV", "V"]
    for idx, (ten, cau_phan) in enumerate(cac_phan):
        if not cau_phan:
            continue
        dp = tinh_diem_cau(cau_phan)
        top3 = sorted(sorted(dp, key=lambda x: x[2], reverse=True)[:3], key=lambda x: x[0])
        ket_qua.append(f"**{so_la_ma[idx]}. {ten}**")
        for _, cau, _ in top3:
            ket_qua.append(f"- {cau[:200]}{'...' if len(cau) > 200 else ''}")
        ket_qua.append("")
    ket_qua += ["## 💡 Điểm quan trọng cần nhớ"]
    for _, cau, _ in top5:
        ket_qua.append(f"- {cau[:200]}{'...' if len(cau) > 200 else ''}")
    ket_qua += ["", "## ✅ Kết luận"]
    cuoi = " ".join(cac_cau[-2:])
    ket_qua.append(cuoi[:300] + ("..." if len(cuoi) > 300 else ""))
    return "\n".join(ket_qua)


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/lay-loi-thoai", methods=["POST"])
def lay_loi_thoai():
    data = request.get_json()
    link = (data.get("link") or "").strip()
    if not link:
        return jsonify({"loi": "Vui lòng nhập link video."}), 400

    nen_tang = nhan_dien_nen_tang(link)
    if not nen_tang:
        return jsonify({"loi": "Link không được hỗ trợ. Vui lòng dùng YouTube, TikTok hoặc Facebook."}), 400

    try:
        if nen_tang == "youtube":
            toan_bo, ngon_ngu = lay_youtube(link)
        else:
            toan_bo, ngon_ngu = lay_khac(link)

        van_ban = dinh_dang_doan(toan_bo)
        return jsonify({
            "nen_tang": nen_tang,
            "ngon_ngu": ngon_ngu,
            "van_ban": van_ban,
            "so_ki_tu": len(van_ban),
            "so_doan": van_ban.count("\n\n") + 1,
        })
    except TranscriptsDisabled:
        return jsonify({"loi": "Video YouTube đã tắt phụ đề."}), 400
    except NoTranscriptFound:
        return jsonify({"loi": "Không tìm thấy phụ đề YouTube."}), 400
    except Exception as e:
        return jsonify({"loi": f"Lỗi: {str(e)}"}), 500


@app.route("/tom-tat", methods=["POST"])
def tom_tat():
    data = request.get_json()
    van_ban = (data.get("van_ban") or "").strip()
    if not van_ban:
        return jsonify({"loi": "Không có nội dung để tóm tắt."}), 400
    try:
        return jsonify({"tom_tat": tao_dan_y(van_ban)})
    except Exception as e:
        return jsonify({"loi": f"Lỗi: {str(e)}"}), 500


@app.route("/phan-tich-viral", methods=["POST"])
def phan_tich_viral_route():
    data = request.get_json()
    van_ban = (data.get("van_ban") or "").strip()
    nen_tang = (data.get("nen_tang") or "youtube").strip()
    if not van_ban:
        return jsonify({"loi": "Không có nội dung để phân tích."}), 400
    try:
        bao_cao = tao_bao_cao_viral(van_ban, nen_tang)
        return jsonify(bao_cao)
    except Exception as e:
        return jsonify({"loi": f"Lỗi: {str(e)}"}), 500


# ─────────────────────────────────────────
# TỪ KHÓA & HASHTAG (offline)
# ─────────────────────────────────────────
STOP_WORDS_KW = {
    "là","và","của","có","được","cho","với","trong","này","đó","một","các","những",
    "thì","mà","để","đã","sẽ","không","về","hay","cũng","như","từ","khi","nên","vì",
    "theo","bởi","lại","ra","vào","lên","đến","còn","rồi","nếu","nhưng","tuy","hoặc",
    "tôi","bạn","anh","chị","mình","họ","nó","ta","chúng","thôi","thật","rất","quá",
    "the","a","an","is","are","was","were","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall","must",
    "and","or","but","if","in","on","at","to","for","of","with","by","from","up",
    "about","into","through","after","before","that","this","it","he","she","they",
    "we","you","i","me","him","her","us","them","my","your","his","its","our","their",
}

def trich_xuat_tu_khoa(van_ban, so_luong=30):
    tu_list = re.findall(r'\b\w{3,}\b', van_ban.lower())
    tu_loc = [w for w in tu_list if w not in STOP_WORDS_KW and not w.isdigit()]
    tan_suat = Counter(tu_loc)
    top = tan_suat.most_common(so_luong)

    # Tìm cụm 2 từ xuất hiện nhiều
    words = van_ban.lower().split()
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)
               if words[i] not in STOP_WORDS_KW and words[i+1] not in STOP_WORDS_KW
               and len(words[i]) > 2 and len(words[i+1]) > 2]
    bigram_count = Counter(bigrams).most_common(15)
    bigram_top = [(bg, c) for bg, c in bigram_count if c >= 2]

    tu_khoa_don = [{"tu": w, "so_lan": c} for w, c in top[:20]]
    tu_khoa_cum = [{"tu": bg, "so_lan": c} for bg, c in bigram_top[:10]]
    hashtags = ["#" + re.sub(r'\s+', '_', w).strip("_") for w, _ in (top[:15] + bigram_top[:5])]

    return {
        "tu_khoa_don": tu_khoa_don,
        "tu_khoa_cum": tu_khoa_cum,
        "hashtags": hashtags,
    }


@app.route("/tu-khoa", methods=["POST"])
def tu_khoa_route():
    data = request.get_json()
    van_ban = (data.get("van_ban") or "").strip()
    if not van_ban:
        return jsonify({"loi": "Không có nội dung."}), 400
    try:
        return jsonify(trich_xuat_tu_khoa(van_ban))
    except Exception as e:
        return jsonify({"loi": f"Lỗi: {str(e)}"}), 500


# ─────────────────────────────────────────
# DỊCH THUẬT (deep-translator, miễn phí)
# ─────────────────────────────────────────
@app.route("/dich", methods=["POST"])
def dich_route():
    data = request.get_json()
    van_ban = (data.get("van_ban") or "").strip()
    ngon_ngu_dich = (data.get("ngon_ngu_dich") or "en").strip()
    if not van_ban:
        return jsonify({"loi": "Không có nội dung để dịch."}), 400

    try:
        from deep_translator import GoogleTranslator
        # Dịch theo từng đoạn 4500 ký tự (giới hạn Google)
        CHUNK = 4500
        cac_doan = [van_ban[i:i+CHUNK] for i in range(0, len(van_ban), CHUNK)]
        ket_qua = []
        translator = GoogleTranslator(source="auto", target=ngon_ngu_dich)
        for doan in cac_doan:
            ket_qua.append(translator.translate(doan))
        return jsonify({"ban_dich": "\n\n".join(filter(None, ket_qua))})
    except Exception as e:
        return jsonify({"loi": f"Lỗi dịch: {str(e)}"}), 500


# ─────────────────────────────────────────
# GỢI Ý CẢI THIỆN VIRAL
# ─────────────────────────────────────────
def tao_goi_y_viral(bao_cao):
    """Dựa vào điểm từng yếu tố, đưa ra gợi ý viết lại cụ thể."""
    goi_y = []
    yeu_to_map = {y["ten"]: y for y in bao_cao["cac_yeu_to"]}

    # Hook
    hook_diem = yeu_to_map.get("🎣 Hook (10 giây đầu)", {}).get("diem", 100)
    if hook_diem < 60:
        goi_y.append({
            "van_de": "🎣 Hook yếu — người xem sẽ vuốt qua trong 3 giây đầu",
            "cach_sua": [
                "Mở đầu bằng câu hỏi kích thích: \"Bạn có biết tại sao 90% người làm X đều thất bại?\"",
                "Dùng con số gây sốc ngay câu đầu: \"Tôi đã mất 6 tháng để học điều này — bạn chỉ cần 3 phút\"",
                "Tiết lộ ngay điều bí mật: \"Sự thật mà không ai dám nói về [chủ đề video]\"",
                "Tạo kịch tính: \"Điều tôi sắp nói có thể khiến nhiều người ghét tôi, nhưng...\"",
            ]
        })

    # Cảm xúc
    cx_diem = yeu_to_map.get("❤️ Cảm xúc & Kích thích", {}).get("diem", 100)
    if cx_diem < 40:
        goi_y.append({
            "van_de": "❤️ Nội dung quá khô — thiếu từ ngữ kích thích cảm xúc",
            "cach_sua": [
                "Thêm từ ngữ tạo tò mò: \"bí mật\", \"sự thật\", \"không ai biết\", \"lần đầu tiên\"",
                "Khai thác nỗi sợ/nỗi đau: \"Nếu bạn không làm điều này, bạn sẽ bị bỏ lại phía sau\"",
                "Tạo cảm giác khẩn cấp: \"Chỉ còn [X] ngày\", \"Đừng để quá muộn\"",
                "Dùng từ cảm xúc mạnh: \"choáng ngợp\", \"không thể tin được\", \"thay đổi hoàn toàn\"",
            ]
        })

    # Giá trị
    gt_diem = yeu_to_map.get("💎 Giá trị & Đồng cảm", {}).get("diem", 100)
    if gt_diem < 40:
        goi_y.append({
            "van_de": "💎 Thiếu giá trị thực tiễn — người xem không biết mình được gì",
            "cach_sua": [
                "Nói rõ người xem sẽ học được gì: \"Sau video này bạn sẽ biết cách...\"",
                "Dùng format \"X bước\", \"X mẹo\", \"X sai lầm\" — rõ ràng và dễ theo dõi",
                "Nói trực tiếp vào vấn đề của người xem: \"Nếu bạn đang gặp phải... thì video này dành cho bạn\"",
                "Thêm ví dụ thực tế, con số cụ thể thay vì nói chung chung",
            ]
        })

    # CTA
    cta_diem = yeu_to_map.get("📣 Lời kêu gọi (CTA)", {}).get("diem", 100)
    if cta_diem < 40:
        goi_y.append({
            "van_de": "📣 Thiếu CTA — người xem xem xong rồi... biến mất",
            "cach_sua": [
                "Nhắc like/share ít nhất 3 lần: đầu video, giữa và cuối",
                "Giải thích LÝ DO tại sao nên like: \"Nếu video này hữu ích, like để tôi biết làm thêm nội dung tương tự\"",
                "Kêu gọi comment bằng câu hỏi: \"Bạn đang gặp vấn đề nào? Comment bên dưới nhé!\"",
                "Gợi ý video tiếp theo: \"Xem video này tiếp theo nếu bạn muốn biết thêm về...\"",
            ]
        })

    # Kể chuyện
    kc_diem = yeu_to_map.get("📖 Kể chuyện (Storytelling)", {}).get("diem", 100)
    if kc_diem < 30:
        goi_y.append({
            "van_de": "📖 Thiếu yếu tố kể chuyện — nội dung rời rạc, khó đọng lại",
            "cach_sua": [
                "Mở đầu bằng 1 câu chuyện cá nhân: \"Hồi đó tôi cũng từng thất bại với...\"",
                "Dùng cấu trúc: Vấn đề → Hành trình → Giải pháp → Kết quả",
                "Thêm chi tiết cụ thể: tên người, địa điểm, thời gian — giúp câu chuyện đáng tin hơn",
                "Kết thúc bằng thông điệp rõ ràng, không chỉ dừng đột ngột",
            ]
        })

    if not goi_y:
        goi_y.append({
            "van_de": "✅ Video đã khá tốt trên nhiều mặt",
            "cach_sua": [
                "Tối ưu thumbnail — thường chiếm 50% quyết định click",
                "Đăng đúng giờ vàng (7-9h sáng hoặc 20-22h tối)",
                "Tương tác với 50 comment đầu tiên trong 1 tiếng sau đăng",
                "Ghim comment tóm tắt nội dung để giữ người xem lâu hơn",
            ]
        })

    return goi_y


@app.route("/goi-y-viral", methods=["POST"])
def goi_y_viral_route():
    data = request.get_json()
    van_ban = (data.get("van_ban") or "").strip()
    nen_tang = (data.get("nen_tang") or "youtube").strip()
    if not van_ban:
        return jsonify({"loi": "Không có nội dung."}), 400
    try:
        bao_cao = tao_bao_cao_viral(van_ban, nen_tang)
        goi_y = tao_goi_y_viral(bao_cao)
        return jsonify({"goi_y": goi_y, "diem_tong": bao_cao["diem_tong"]})
    except Exception as e:
        return jsonify({"loi": f"Lỗi: {str(e)}"}), 500


if __name__ == "__main__":
    print("\n✅ Web app đang chạy tại: http://localhost:5000\n")
    app.run(debug=False, port=5000)
