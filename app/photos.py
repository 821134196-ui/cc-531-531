"""照蛋照片：本地生成 SVG 文件（真实部署替换为相机上传，仍落本地目录）。"""
import os

PHOTO_KINDS = {
    "overview": "整盘照蛋",
    "fertile": "受精：可见血管网",
    "stopped": "停育：血环",
    "broken": "破损：裂纹",
    "infertile": "无精：透亮无血管",
}

_TMPL = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="360" viewBox="0 0 480 360">
<rect width="480" height="360" fill="#26242A"/>
<rect x="12" y="12" width="456" height="336" rx="10" fill="none" stroke="#3D3B40" stroke-width="2"/>
<ellipse cx="240" cy="190" rx="130" ry="150" fill="{glow}"/>
<ellipse cx="240" cy="190" rx="118" ry="138" fill="{inner}"/>
{marks}
<text x="24" y="40" font-family="sans-serif" font-size="16" fill="#C9C4BC">{label}</text>
<text x="24" y="332" font-family="monospace" font-size="13" fill="#8A857D">{tray} · {ts}</text>
</svg>"""

_VESSELS = """
<circle cx="240" cy="185" r="10" fill="#3a1d1a"/>
<path d="M240 185 C210 160 185 150 150 150 M240 185 C270 160 295 150 330 152
         M240 185 C208 205 180 220 150 232 M240 185 C272 208 300 222 332 230
         M240 185 C236 150 238 120 240 96 M240 185 C244 220 246 250 240 282"
      stroke="#5a2b22" stroke-width="3" fill="none" stroke-linecap="round"/>"""

_BLOOD_RING = """
<ellipse cx="240" cy="190" rx="86" ry="104" fill="none" stroke="#8e2f22" stroke-width="7"/>
<ellipse cx="240" cy="190" rx="86" ry="104" fill="none" stroke="#b8452f" stroke-width="2"
 stroke-dasharray="6 8"/>"""

_CRACK = """
<path d="M205 92 L226 130 L214 158 L238 190 L224 222 L244 252 L236 290"
 stroke="#f2d9b0" stroke-width="3" fill="none" stroke-linejoin="round"/>
<path d="M226 130 L250 122 M214 158 L190 172 M238 190 L262 196 M224 222 L204 240"
 stroke="#f2d9b0" stroke-width="2" fill="none"/>"""

_EMPTY = """
<ellipse cx="240" cy="190" rx="70" ry="90" fill="#caa46a" opacity="0.18"/>"""


def render_svg(kind: str, tray_code: str, ts: str) -> str:
    if kind == "fertile":
        glow, inner, marks, label = "#7a3a22", "#5a2b1e", _VESSELS, PHOTO_KINDS[kind]
    elif kind == "stopped":
        glow, inner, marks, label = "#6e2c20", "#4a201a", _BLOOD_RING, PHOTO_KINDS[kind]
    elif kind == "broken":
        glow, inner, marks, label = "#6d5a3e", "#4a3d2a", _CRACK, PHOTO_KINDS[kind]
    elif kind == "infertile":
        glow, inner, marks, label = "#8a6f45", "#6b5636", _EMPTY, PHOTO_KINDS[kind]
    else:
        glow, inner, marks, label = "#7d4626", "#57301d", _VESSELS + _BLOOD_RING, "整盘照蛋留档"
    return _TMPL.format(glow=glow, inner=inner, marks=marks, label=label,
                        tray=tray_code, ts=ts)


def save_photo(base_dir: str, candling_id: int, kind: str,
               tray_code: str, ts: str) -> str:
    folder = os.path.join(base_dir, "photos")
    os.makedirs(folder, exist_ok=True)
    name = f"c{candling_id:04d}_{kind}.svg"
    with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
        f.write(render_svg(kind, tray_code, ts))
    return f"photos/{name}"
