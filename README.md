# 纽约分部 · SQLite 作业库每日备份

一个可本地运行 / Docker Compose 启动的备份演练台：

* 一个样例 SQLite 作业源库（`sample/jobs.sqlite`，启动时复制为 `data/jobs.db`）；
* 一个 Flask 操作页：追加样例记录、设置每日备份时刻、调整演练时钟、按计划日期下载备份；
* 一个后台调度线程：到点自动备份，页面显示计划日期、状态与实际完成时间。

页面语言为中文，所有时刻默认按 **America/New_York** 显示（真实时钟与演练时钟并排）。

---

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据卷：`ny-data` 存放源库与状态库，`ny-backups` 存放备份文件（`/app/backups`）。
第一次启动时 `sample/jobs.sqlite` 会被复制到数据卷里的 `jobs.db`；之后重启不会覆盖已有源库。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate   # 可选
pip install -r requirements.txt
python run.py          # http://localhost:8000
```

`run.py` 会在首次启动时复制样例源库，然后用 waitress 提供服务。
可用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SOURCE_DB` | `data/jobs.db` | 被备份的源库 |
| `STATE_DB` | `data/state.db` | 操作页自身的计划/配置/日志库（不参与备份） |
| `BACKUP_DIR` | `backups` | 备份输出目录 |
| `TICK_SECONDS` | `1` | 调度轮询间隔（秒） |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `DISABLE_SCHEDULER` | 未设置 | 设为 `1` 可关闭后台调度（测试用） |

### 测试

```bash
pip install pytest
python -m pytest tests -q
```

---

## 行为说明

### 计划日期与到点判定

* 每个备份计划以其 **纽约本地日历日期** 为键，例如 `2026-11-21`，文件名为
  `jobs-2026-11-21.sqlite`。
* 每日设定时刻（默认 `02:30`）按纽约时区解析为绝对 UTC 时刻：
  * **春季跳时**：设定时刻不存在（如 02:30）时，顺延到跳时后的第一个有效分钟
    （→ 03:00 EDT），页面标注“顺延”；
  * **秋季重复**：设定时刻出现两次（如 01:30）时取第一次（EDT）。
* 调度线程每秒轮询；页面显示的“实际完成时间”来自计划记录，同时保留真实完成时间
  （文件 mtime），重启后不变。

### 停机补跑

* 服务重新运行后，若已错过多个到期时刻，**只补最近一次**，并仍归在其原计划日期；
  更早的错过计划记录为 `skipped`（页面可见原因），不会生成备份文件。
* 补跑期间源库仍可写入；页面可随时追加记录。

### 备份文件

* 快照使用 SQLite `VACUUM INTO`：在一个读事务内完成，输出是 **同一时点** 的独立数据库
  （`journal_mode=delete`，无 `-wal`/`-shm` 伴随文件），可以单独拷走打开。
* 写入过程先落到隐藏的 `.<name>.<token>.tmp`，校验（`integrity_check`、
  `foreign_key_check`、写入计数一致性）通过并 `fsync` 后，用 `os.replace` 原子发布为公开文件名。
* **残件不供下载**：`.tmp` / `.partial` 等文件不会被列出，也不会被下载路由命中；
  启动时会清理残件。
* **重启后沿用完整文件**：已完整落盘的备份不会重写（哈希、mtime 不变），
  状态库丢失时也会重新登记（adopt）已有文件。
* 同一天的备份已完成后，点击“立即备份”不会覆盖原文件。

### 演练时钟

页面提供三种方式：

1. 输入偏移秒数（正数向未来，负数向过去）；
2. 跳转到某个纽约本地时间；
3. 复位为真实时间。

演练时钟是到期判定与页面显示的权威时间；真实时钟始终单独显示。

---

## 目录结构

```
nybackup/
  schedule.py   纽约时区计划时刻解析（跳时/重复）
  store.py      状态库：配置、计划、事件
  source.py     源库 schema / 追加样例记录
  backup.py     VACUUM INTO 快照 + 原子发布 + 校验
  service.py    调度、补跑、重启恢复
  web.py        Flask 路由与页面
  serve.py      waitress 入口
templates/index.html
static/style.css
tools/make_sample.py   重新生成样本源库
sample/jobs.sqlite     随附样例源库
tests/                 pytest 测试
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 操作页（计划列表、追加、设置、时钟、事件） |
| POST | `/append` | 追加一条样例作业 |
| POST | `/settings` | 设置每日备份时刻 `backup_time=HH:MM` |
| POST | `/clock` | 设置演练时钟偏移 `offset_seconds=…` |
| POST | `/clock/jump` | 跳转演练时钟 `target=YYYY-MM-DDTHH:MM`（纽约本地） |
| POST | `/clock/reset` | 复位演练时钟 |
| POST | `/run/<YYYY-MM-DD>` | 立即执行某计划 |
| GET | `/download/<YYYY-MM-DD>` | 下载该计划日期的备份（未完成则 404） |
| GET | `/api/status` | JSON：虚拟/真实时间、配置、计划、可用文件 |
| GET | `/healthz` | 健康检查 |

## 说明与限制

* 单实例设计：调度线程运行在 Web 进程内，请勿对同一 `BACKUP_DIR` 同时启动多个实例。
* 备份为整库快照；源库很大时快照期间会占用临时磁盘空间（约等于源库大小）。
* 生产部署建议把 `BACKUP_DIR` 挂到独立卷并配合外部备份/监控。
