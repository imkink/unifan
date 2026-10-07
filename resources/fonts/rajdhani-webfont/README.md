# Rajdhani 精简 Webfont

包含 SemiBold（600）、Bold（700）的 WOFF2，来自 Google Fonts 官方仓库：
https://github.com/google/fonts/tree/main/ofl/rajdhani

## 使用

保持 CSS 和两份 WOFF2 在同一目录，网页中引用：

```html
<link rel="stylesheet" href="/fonts/rajdhani-webfont/rajdhani.css">
```

```css
.device-screen { font-family: "Rajdhani", sans-serif; font-weight: 600; }
.device-value { font-weight: 700; }
```

没有网络依赖。CSS 使用相对路径，上传整个目录即可。服务器建议为 WOFF2 返回
`Content-Type: font/woff2`。中文等范围外的字符由后备字体显示。

## 保留字符

- ASCII U+0000–U+007F 范围内原字体已有的字形，完整包含可打印字符 U+0020–U+007E。
- 不换行空格 U+00A0。
- `° ± · × ÷ Ω ‰ Ω ∆ − ≤ ≥`。

原字体没有大多数 ASCII 控制码的绘制字形；换行和制表等由浏览器排版处理。
保留源字体已有的 U+0000、U+000D 映射，没有为其余控制码虚构字形。
新增 U+03A9 `Ω` 到已有 U+2126 `Ω` 字形的映射，两者在 Unicode 中规范等价。
没有描摹或改变任何原始矢量轮廓。其余字符编码均已剔除；字体内部仍保留必需的
`.notdef` 等辅助字形。保留字距信息，不保留可选连字替换。

单位如 Mbps、Gbps、KB、MB、GB、TB、Hz、kHz、MHz、GHz、V、A、mA、W、kW、
Wh、kWh、dB、dBm、rpm、ms、cm、mm 由 ASCII 字母组合，无需额外字形。

原字体缺少 µ、μ、℃、℉、¹、²、³、′、″ 等字形，因此没有纳入。
温度写作 `25 °C` / `77 °F`；面积、体积可写作 `m<sup>2</sup>` / `m<sup>3</sup>`。
这些单一 Unicode 字符若直接使用，将由后备字体显示。

## 文件和验证

`manifest.json` 记录实际保留的码点、体积和 SHA-256；`build.py` 是可重复运行的
子集生成程序，需要 `fonttools[woff]`，接受 `--bold` 和 `--semibold` 两个原版 TTF 路径。
生成过程检查 WOFF2 解压、字符覆盖、字重和原始字距一致性。

授权为 SIL Open Font License 1.1，随包附官方 `OFL.txt`。
