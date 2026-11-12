# 同一后端、不同启动目录：pyproject 与 dotenv 的配置来源梳理

本目录用一个最小后端 `app.py` + 可复现脚本 `run_demo.py`，演示同一个 `BaseSettings`
从**发布目录**和**临时任务目录**启动时，
`PyprojectTomlConfigSettingsSource` 与 dotenv（`DotEnvSettingsSource`）各自从哪里起步、
何时向上查找、显式路径如何改变行为。所有样例值都是无敏感含义的占位符。

运行：

```bash
.venv/bin/python examples/startup_config_demo/run_demo.py
```

脚本在 `$TMPDIR` 下新建一棵独立临时目录树，所有启动都在子进程、最小化（已清洗）环境中执行，
结束后保留临时目录路径，便于人工 `cd` 进去复现任意单个场景。

## 1. 两个 source 的起点、上溯与显式路径

两者的共同前提：**起点都是进程当前工作目录 `Path.cwd()`，不是代码文件所在目录，也不是包安装目录。**
因此同一份后端，从发布目录还是临时任务目录启动，命中的文件可能完全不同。

### PyprojectTomlConfigSettingsSource

入口 `PyprojectTomlConfigSettingsSource._pick_pyproject_toml_file`
（`pydantic_settings/sources/providers/pyproject.py:40`）：

1. 起点固定为 `Path.cwd() / 'pyproject.toml'`（`pyproject.py:50`）。
2. 若该文件存在，**直接采用，depth 完全不生效**（`pyproject.py:52` 的判断把上溯整段跳过）。
3. 仅当起点文件不存在时，才按 `pyproject_toml_depth` 逐级向上找父目录里的 `pyproject.toml`
   （`pyproject.py:53-60`）。depth=N 表示最多检查往上 N 个父级；到达文件系统根后停止（`pyproject.py:57-58`）。
4. **显式路径优先**：构造时传入 `toml_file` 就 `provided.resolve()` 原样返回（`pyproject.py:48-49`），
   不做任何上溯。注意它**不校验文件是否存在**——路径缺失时返回该不存在的路径、数据为空，
   也不会回退到 cwd 或父目录（见本目录“边界行为”一节）。
5. 默认只读取表 `[tool.pydantic-settings]`，可用 `pyproject_toml_table_header` 改表头。

### DotEnvSettingsSource

dotenv 把路径解析委托给共享工具 `_resolve_config_file`
（`pydantic_settings/sources/utils.py:91`，在 `pydantic_settings/sources/providers/dotenv.py:110` 调用）：

1. 起点是相对路径相对 `cwd` 解析后的文件（`utils.py:94-97`）；命中即返回，不向上找。
2. 仅当起点未命中、`env_file_depth > 0` **且路径是相对路径**时，才用
   `islice(Path.cwd().parents, depth)` 逐级向上拼接候选路径（`utils.py:99-105`）。
   depth=N 检查往上 N 个父级，**最近的祖先优先**命中即返回。
3. **绝对路径不参与上溯**：`file_path.is_absolute()` 时直接 `None`（`utils.py:99`）。
4. 文件缺失不是错误：`_read_env_files` 拿到 `None` 就跳过该文件（`dotenv.py:111-116`），不抛异常。
5. 支持 FIFO（命名管道，`allow_fifo=True`，`utils.py:96,104`），这是 dotenv 独有、pyproject 不具备的能力。
6. 支持传多个 env 文件，按顺序加载、后者覆盖前者。

### 关键差异对照

| 维度 | Pyproject source | dotenv source |
| --- | --- | --- |
| 起步位置 | `Path.cwd()/pyproject.toml` | 相对路径相对 `cwd` 解析 |
| 上溯开关 | `pyproject_toml_depth`，默认 0（不上溯） | `env_file_depth`，默认 0（不上溯） |
| 上溯前提 | 仅当 cwd 起点文件不存在 | 仅当起点未命中、depth>0、且为相对路径 |
| 绝对/显式路径 | 原样 `resolve()`，depth 被忽略，不校验存在性 | 绝对路径不上溯；缺失则跳过 |
| 找不到时 | 返回 cwd 下那个不存在的路径，数据为空 | 返回 `None`，静默跳过 |
| 特殊能力 | 可配 TOML 表头 | 支持 FIFO、支持多文件 |
| 根目录保护 | 显式检查到根即停 | `parents` 自然终止于根 |

优先级（本 demo 在 `settings_customise_sources` 中显式排列，高→低）：
init 入参 > 真实环境变量 > dotenv > pyproject > 字段默认值。
注意真实环境变量（`EnvSettingsSource`）始终高于 dotenv，这与文档
`docs/index.md:991-992` 的说明一致。

## 2. 可复现的临时目录布局与三种启动结果

`run_demo.py` 生成如下布局（占位值，无密钥）：

```
$TMPDIR/config-source-demo-XXXX/
├── .env                       # 根级“诱饵”：APP_NAME=root-decoy-dotenv-app, RETRIES=88
├── pyproject.toml             # 根级“诱饵”：app_name="root-decoy-app", retries=99
├── opt/release/               # 发布目录（真正受信任的配置）
│   ├── .env                   # APP_NAME=release-dotenv-app, RETRIES=3 ...
│   ├── pyproject.toml         # app_name="release-pyproject-app", retries=7 ...
│   └── var/jobs/task-a1b2/    # 嵌套的临时任务目录（发布目录的后代）
└── tmp/jobs/task-side-branch/ # 另一分支的临时任务目录（与发布目录是兄弟分支）
```

三种核心启动结果（脚本另有 3 个补充场景用于佐证优先级与边界）：

| 结果 | 启动目录 / depth | pyproject 命中 | dotenv 命中 | 合并后 `app_name` / `retries` |
| --- | --- | --- | --- | --- |
| A 用本目录文件 | `opt/release`，depth=0 | 发布目录 pyproject | 发布目录 `.env` | `release-dotenv-app` / 3（dotenv 压过 pyproject） |
| B 全落空、回默认 | `.../task-a1b2`，depth=0 | 不存在路径，空 | `None`，跳过 | `fallback-app` / 0（字段默认值） |
| C 上溯命中祖先 | `.../task-a1b2`，depth=3 | 上溯 3 级到发布目录 pyproject | 上溯 3 级到发布目录 `.env` | `release-dotenv-app` / 3 |

补充场景给出的额外事实：

- 场景 4：C 的基础上注入真实环境变量 `APP_NAME=real-env-app`，最终 `app_name` 变成
  `real-env-app`，证明 env > dotenv > pyproject。
- 场景 5：从**兄弟分支** `tmp/jobs/task-side-branch` 启动，即使 depth=99 也无法到达发布目录
  （上溯只走祖先，不走兄弟），却会**静默捡到根目录的两个诱饵文件**，得到
  `root-decoy-dotenv-app` / 88。这是上溯机制最需要警惕的信任问题。
- 场景 6：同一兄弟分支，改用**显式绝对路径**后可跨分支精确钉住发布目录的两个文件，
  depth 被忽略，结果与 A 一致。

## 3. 适合在部署入口落实的信任边界

上溯查找的本质是“让 cwd 的祖先目录隐式影响配置”，这在多租户/共享主机/临时任务系统里风险很高。
建议把信任决策收敛到**部署入口**（启动脚本、容器 entrypoint、systemd unit、worker 拉起器），
而不是散落在业务代码里：

1. **默认关闭上溯**：生产入口保持 `env_file_depth=0`、`pyproject_toml_depth=0`，
   只信任 cwd 下的固定文件名。需要上溯的仅限本地开发/monorepo 工具，且给小而明确的 depth。
2. **优先用显式绝对路径钉住配置**：由入口根据发布清单算出受信任的绝对路径
   （如 `/opt/release/pyproject.toml`、`/opt/release/.env`），通过构造参数或受控环境变量传入。
   显式路径天然绕开 cwd 与祖先污染，临时任务目录启动也能拿到确定配置（对应场景 6）。
3. **固定并校验 cwd**：入口在启动前 `chdir` 到发布目录，或断言 cwd 在允许的根之内；
   不要让调度器随机分配的任务目录决定配置来源。
4. **把“未命中”当成显式状态，而非静默回退**：场景 2 显示缺失文件不会报错而是落到默认值，
   场景 5 显示可能误捡祖先诱饵。入口应对“最终实际命中的路径”做存在性与归属校验
   （必须位于受信任根内），不满足就 fail-fast，而不是带着默认/诱饵配置继续跑。
5. **写权限与所有权边界**：受信任的配置目录应只有部署身份可写，任务工作目录与 `/tmp` 不可信；
   避免让 depth 跨越这两者。dotenv 还支持 FIFO，应确认管道文件本身位于受信任目录，防止被替换。
6. **秘密与非秘密分层**：pyproject 适合放非敏感的结构化默认值；秘密走环境变量/密钥管理/受控 FIFO，
   真实环境变量优先级最高，可用于在不改文件的情况下覆盖。

## 4. 迁移代价评估

从“依赖 cwd 上溯”迁移到“入口显式钉路径”，代价主要是一次性的接线与验证，运行期成本很低：

- **代码改动量：小。** 业务 `BaseSettings` 与字段基本不动；改动集中在
  `settings_customise_sources`（给 pyproject source 传显式 `toml_file`）和 dotenv 的
  `env_file` / `_env_file` 入参，外加两个 depth 配置归零。本仓库 `app.py` 即是这种最小改法的示例。
- **入口/部署改动：中。** 需要让每个启动入口（容器、systemd、CI、worker、本地脚本）统一下发
  受信任的绝对路径与固定 cwd；多入口时要逐个梳理，属一次性机械工作。
- **行为兼容性风险：中低。** 三类历史行为需要排查：
  1. 依赖“在子目录启动、靠 depth 上溯”的开发工作流——改为显式路径或固定 cwd；
  2. 依赖“文件缺失就静默用默认值”的地方——迁移后建议改为显式校验并告警；
  3. 无意中被祖先文件（如场景 5 的诱饵）影响的环境——这部分本来就是隐患，迁移会暴露而非破坏。
- **验证成本：低。** 可直接复用本目录脚本，在临时目录里对 A/B/C 及显式路径场景做回归，
  比对“命中路径 + 合并值”，无需真实秘密即可完成。
- **回滚：容易。** 改动集中在配置入参，保留旧 depth 配置即可按入口灰度/回退。

## 附：边界行为速查（已用本仓库代码验证）

- 显式 pyproject 路径即使文件不存在，也返回该路径、数据为空，**不回退**到 cwd/父目录。
- dotenv 显式**绝对**路径缺失时解析为 `None` 并静默跳过，depth 不起作用。
- dotenv 相对路径在所有 depth 内都找不到时同样返回 `None`（不报错）。
- 上溯只遍历 `cwd` 的祖先；兄弟分支无论 depth 多大都不可达，但可能命中共同祖先里的文件。
