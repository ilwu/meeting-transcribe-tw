"""逐字稿比對共用的文字工具：正規化與字元錯誤率（CER）。"""
import re

import opencc

_cc = opencc.OpenCC("t2s")


def norm(t):
    """轉簡體、去標點與空白、轉小寫，讓繁簡與標點差異不算錯。"""
    return re.sub(r"[^\w]", "", _cc.convert(t)).lower()


def cer(a, b):
    """以 a 為參考的字元錯誤率：編輯距離 / len(a)，逐列滾動計算以省記憶體。"""
    if not a:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)
