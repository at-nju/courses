# NJU Courses

南京大学全校课程查询接口的非官方公开镜像。

仓库通过 GitHub Actions 定期访问学校课程查询系统，并将接口返回的分页 JSON **原样**保存到 `data/`。脚本不清洗字段、不重命名字段、不合并页面，也不生成其他格式。

> 本项目与南京大学官方无隶属或授权关系。数据可能延迟、缺失或发生变化，请以学校官方系统为准。

## 更新频率

- 每天：刷新最近两个有数据的学期。
- 每月 1 日：从 2000 学年起探测所有学期，并对所有有数据的学期执行全量刷新。
- 数据没有变化时：不创建 Git commit。

GitHub Actions 的 cron 使用 UTC；实际执行可能因 GitHub 调度而略有延迟。

## 数据结构

```text
data/
├── 2025-2026-2/
│   ├── page_001.json
│   ├── page_002.json
│   └── ...
└── 2026-2027-1/
    ├── page_001.json
    ├── page_002.json
    └── ...
```

每个文件都是对应分页请求的完整响应正文。文件名中的页码从 1 开始，单页请求大小为 500 条。

可以直接通过 GitHub 或 `raw.githubusercontent.com` 下载文件。例如：

```text
https://raw.githubusercontent.com/at-nju/courses/main/data/2026-2027-1/page_001.json
```

## 自动任务

Workflow 位于：

```text
.github/workflows/scrape.yml
```

它支持：

- 定时运行；
- 在 Actions 页面手动选择 `latest` 或 `all`；
- 抓取前运行测试；
- 只有 `data/` 发生变化时才由 `github-actions[bot]` 提交。

仓库需要配置 Actions Secret：

```text
NJU_CASTGC
```

Secret 可以是单独的 `CASTGC` 值，也可以是包含 `CASTGC=...` 的 Cookie 字符串。不要将它写入代码、日志或仓库文件。

Cookie 失效后，Action 会失败且不会提交不完整数据。重新取得 `CASTGC` 后，在仓库的 Actions Secrets 中更新同名 Secret，再手动运行一次 `latest` 即可。

## 本地运行

要求 Python 3.11 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
export NJU_CASTGC='your-castgc-value'
```

刷新最近两个学期：

```bash
python scripts/scrape_courses.py --latest 2
```

刷新所有可探测到的学期：

```bash
python scripts/scrape_courses.py --all
```

刷新指定学期：

```bash
python scripts/scrape_courses.py --semester 2026-2027-1
```

运行测试：

```bash
python -m pytest
```

## 可选配置

以下环境变量通常不需要修改：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NJU_START_YEAR` | `2000` | 月度全量探测的起始学年 |
| `NJU_SEMESTER_TERMS` | `1,2` | 需要探测的学期后缀 |
| `NJU_REQUEST_DELAY` | `1.5` | 课程接口请求之间的最小间隔，单位秒 |
| `NJU_REQUEST_TIMEOUT` | `30` | 单次请求超时，单位秒 |

命令行参数会覆盖相应默认值。

## 实现说明

抓取流程如下：

1. 将 `NJU_CASTGC` 写入仅针对 `authserver.nju.edu.cn/authserver` 的会话 Cookie。
2. 打开课程应用入口，让 CAS 自动签发 service ticket 并建立 eHall 会话。
3. 按网页启动流程加载应用权限配置，并初始化教务角色。
4. 按学期和页码顺序请求全校课程接口。
5. 在 `data/.staging/` 中完成整个学期的下载。
6. 全部页面成功后才替换正式学期目录。
7. Git 判断文件是否变化；无变化时不提交。

如果登录失效、返回 HTML、JSON 结构异常或网络请求最终失败，脚本会以非零状态退出，正式数据目录不会被半成品替换。

## License

代码使用 [GNU General Public License v3.0](LICENSE)。课程数据的权利归其原权利人所有，GPL 不自动适用于抓取结果。
