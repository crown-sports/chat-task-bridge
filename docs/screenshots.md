# README 截图来源

两张图都来自 v0.2.0 现有处理器生成的 HTML，没有改写报告页面。

## 合并报告

安装后在仓库根目录运行：

```sh
chatbridge demo --output demo-output
```

打开 `demo-output/feishu/report.html`，以 **1100 × 950** 浏览器视口截图。
页面显示 6 条输入、5 条保留、1 条重复，月份合计 200.50 和 360。
截图停在分组统计之后，完整报告继续包含保留记录预览。渠道交付由本地模拟器执行。

## 审计报告

在仓库根目录运行以下 Python 代码，输出到一个新目录：

```python
import json
from pathlib import Path

from chatbridge.table_task import process

request = json.loads(Path("examples/audit/request.json").read_text())
result = process(request)
output = Path("audit-preview")
output.mkdir(exist_ok=False)
for name, content in result["files"].items():
    (output / name).write_text(content, encoding="utf-8")
```

打开 `audit-preview/report.html`，以 **1100 × 580** 视口截图。报告列出
1 组冲突键、2 条冲突记录、1 条空键和 1 条重复，包含源文件及行号。
截图文件为 `docs/assets/audit-report.png`，合并图为 `docs/assets/merge-report.png`。

两图均为 Chromium 的实际视口截图。README 不再把设计概念图当作功能预览；
旧图的来源和限制仍保留在 [原设计说明](app-preview.md)。
