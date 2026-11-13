# 设备身份合并与拆分的可逆事实重键

本项目保存虚构算力设施的仓库源表、湖文件与Python读取工具。所有设备、组织和金额均为合成，
不含真实身份、账户凭据、模型记录或额外附件；不做模糊相似匹配或个人身份推断。

## 字段与单位

原事实 fact_id 不可变、source_alias 不覆盖；assertions 为无向等同边的版本声明，
withdrawn=true 使该声明退出。用量整数不按实体合并去重。

material-index.json 记录全部输入内容哈希；fixtures 保存原始资料与配置，
计算输出放 `.workspace-output/`（已在 .gitignore），原始资料不会被覆盖。

## 身份解析与事实重键层

新增代码全部位于 `src/`，仅使用 Python 3.11+ 标准库：

| 模块 | 职责 |
| --- | --- |
| `identity_graph.py` | 版本化身份等同图。每个被接受的新修订生成不可变图版本；按有效边全量重算连通分量（撤回桥边可拆分，不用只能合并的并查集）；canonical 取分量内字典序最小 alias；自环允许但不改分量；同修订号内容不一致与过期修订抛 `IdentityConflict`。 |
| `fact_ledger.py` | 不可变事实多重集合。事实保持输入一行，source_alias 永不覆盖；原样重送幂等；同 fact_id 内容变更抛 `FactConflict`；相同金额的独立事实不去重。 |
| `identity_resolution.py` | 事实解析投影与聚合反向索引。entity 只是投影键；每条投影事实保留原 alias 血缘；图版本变化时按旧/新分量闭包撤销旧实体汇总、加入新实体汇总，生成重键事件并断言金额守恒。 |
| `snapshot_store.py` | 图与聚合联合原子发布。临时文件 + `os.replace` 落盘、fcntl 跨进程写锁、manifest 提升 latest 指针；读者无锁读取并独立复算连通性与哈希，重启或多进程下不会出现"新身份配旧总额"。 |
| `pipeline.py` | 端到端编排与 CLI，输出每个图版本的分量/汇总、每次受影响分量与重键事件、守恒检查和 a-c 环路边反例。 |

### 关键语义区分

* **实体标签变化（重键）**：`kind=entity_label_rekey`，只移动实体键，
  `new_usage_added=0`，撤销总量恒等于记入总量。
* **真实事实修订**：同 fact_id 的用量/来源被改时抛 `FactConflict`，
  以 `kind=fact_revision_conflict` 单独记录并拒绝，绝不静默改写原始事实。
* 身份声明冲突（同 revision 内容不一致、revision 回退）走 `IdentityConflict`，
  与事实冲突分开报告。

### 受影响分量闭包

以实体键发生变化的 alias 为种子，分别在旧图和新图取连通分量并求并集，
再映射回旧/新 canonical 键。合并时吸收方与被吸收方、拆分时旧整体与全部后继实体
都会完整纳入撤销/重记，成员缩小但键名保留的实体也不会漏。

## 运行管线

fixtures 是不可信数据，处理它们的 Python 一律用 `-I` 隔离模式，并通过与数据目录
分离的引导器（`tools/`）显式登记受信代码路径，防止数据目录中的同名模块被 import 劫持：

    python -I tools/run_pipeline.py fixtures/facts.csv \
        fixtures/assertions.jsonl fixtures/identity.json \
        --output-dir .workspace-output/run1

输出含每个图版本的 `components`、`canonical`、`entity_totals`，以及 `rekey_events`
（受影响旧/新实体、撤销与记入金额、守恒标志）和 `counterexample_extra_path`
（额外存在 a-c 路径时撤 b-c 不拆分的对照）。

主线场景：alias a/b/c 用量 2/3/5，边 a-b、b-c 时实体 a 总 10；撤 b-c 后 a 总 5、
c 总 5；再撤 a-b 恢复 2/3/5。每个图版本总额恒为 10。

## 运行测试

    python -I tools/run_tests.py
    # 或标准发现方式（容器默认）
    python -m unittest discover -s tests -v
    python -m compileall -q src tests tools

## 固定容器

    docker build -t identity-rekey:local .
    docker run --rm identity-rekey:local

容器镜像以完整 digest 固定，默认仅检查输入契约并运行全部 unittest；
实现仅依赖标准库，无需新增第三方依赖。
