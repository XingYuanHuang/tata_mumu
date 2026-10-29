# -*- coding: utf-8 -*-
"""清除 uid_list.xlsx 里所有已处理标记（'1'/'available'/'done'），
保留第 1 列的 UID，让小号把整张名单重新巡访一遍。"""
from openpyxl import load_workbook

MARKS = ("1", "available", "done")
wb = load_workbook("uid_list.xlsx")
ws = wb.active

n = 0
for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=2, max_col=ws.max_column):
    for c in row:
        if c.value is not None and str(c.value).strip() in MARKS:
            c.value = None
            n += 1

wb.save("uid_list.xlsx")
print(f"已清除标记 {n} 个（范围: 2~{ws.max_column} 列 × {ws.max_row} 行）")

# 复核
wb2 = load_workbook("uid_list.xlsx", read_only=True)
ws2 = wb2.active
left = sum(1 for row in ws2.iter_rows(min_row=1, max_row=ws2.max_row, min_col=2, max_col=ws2.max_column)
           for c in row if c.value is not None and str(c.value).strip() in MARKS)
uid_cnt = sum(1 for row in ws2.iter_rows(min_col=1, max_col=1)
              for c in row if c.value and len(str(c.value).strip()) >= 8)
wb2.close()
print(f"复核: 剩余标记 {left} 个, UID 数 {uid_cnt}")
