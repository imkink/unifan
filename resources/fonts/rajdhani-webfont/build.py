"""生成 ASCII 与常用单位符号的 WOFF2 字体子集；电脑端需安装 fonttools[woff]。"""
import argparse
import hashlib
import json
from pathlib import Path
from fontTools import subset
from fontTools.ttLib import TTFont

# 额外保留温度、乘除、欧姆、千分号等单位/运算符；减少设备本地网页字体体积。
EXTRA = {0xA0,0xB0,0xB1,0xB7,0xD7,0xF7,0x3A9,0x2030,0x2126,0x2206,0x2212,0x2264,0x2265}
WANTED = set(range(128)) | EXTRA

def build(source, output, weight):
    font = TTFont(source)
    original = font.getBestCmap().copy()
    # 欧姆符号 U+2126 规范化后为大写 Omega U+03A9；给两者建立相同字形映射。
    for table in font['cmap'].tables:
        if table.isUnicode() and 0x2126 in table.cmap:
            table.cmap[0x3A9] = table.cmap[0x2126]
    expected = WANTED & set(font.getBestCmap())
    assert set(range(32,127)) | EXTRA <= expected
    options = subset.Options()
    options.layout_features = ['kern']
    options.notdef_glyph = True
    options.notdef_outline = True
    options.recommended_glyphs = False
    options.name_IDs = [0,1,2,3,4,5,6,13,14,16,17]
    options.name_languages = [0x409]
    worker = subset.Subsetter(options=options)
    worker.populate(unicodes=WANTED)
    worker.subset(font)
    font.flavor = 'woff2'
    font.save(output)
    check = TTFont(output)
    assert set(check.getBestCmap()) == expected
    assert check['OS/2'].usWeightClass == weight
    assert output.read_bytes()[:4] == b'wOF2'
    for cp, name in check.getBestCmap().items():
        old_name = original[0x2126 if cp == 0x3A9 else cp]
        assert check['hmtx'][name] == TTFont(source)['hmtx'][old_name]
    # 回读并解码 WOFF2，检查压缩表可用；上面还验证了字符覆盖、字重和字形宽度。
    check['glyf'].compile(check)
    return {'file':output.name,'weight':weight,'bytes':output.stat().st_size,
            'source_bytes':source.stat().st_size,
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
            'codepoints':[f'U+{cp:04X}' for cp in sorted(expected)],
            'glyph_count':len(check.getGlyphOrder())}

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--bold',required=True,type=Path)
    parser.add_argument('--semibold',required=True,type=Path)
    args=parser.parse_args()
    dest=Path(__file__).resolve().parent
    results=[]
    for source,name,weight in [(args.bold,'Bold',700),(args.semibold,'SemiBold',600)]:
        result=build(source,dest/f'Rajdhani-{name}-ascii-units.woff2',weight)
        results.append(result)
        print(f'{name}: {result["bytes"]} bytes; {len(result["codepoints"])} codepoints; {result["glyph_count"]} glyphs')
    (dest/'manifest.json').write_text(json.dumps(results,indent=2)+'\n')
