# 契约与验证

[文档目录](../index.md) · [English](../../en/development/contracts-and-testing.md)

验证应围绕变更边界展开。响应解析、业务语义校验和完整 agent 复现是不同检查。

## 校验层次

1. **输入／记录契约**：来源形态、带时区时间、身份、instrument／session 与证据引用。
2. **结构化输出**：允许字段、必填值、枚举和嵌套 schema。
3. **业务校验**：价格角色、引用、PM reason、风险／敞口和写入拓扑。
4. **持久化／关联**：记录关系、执行状态、episode reference 和过期哈希。
5. **评估**：算术、时间／成本口径和 provenance。

合法 JSON 仍可能在后续层失败，数值报告正确不证明上游模型推理正确。

## 已有检查

仓库根目录：

```sh
python -B tools/verify_results.py
```

它解析公开 Python，重算部分保存曲线、calendar scaling、time-batch 和 CEAU 统计，不运行 agent，也不验证未公开输入。

安装开发依赖后，在 `code/`：

```sh
uv run pytest tests/test_pm_console_saved_results.py tests/test_pm_console_buy_hold.py
```

覆盖 saved-mode 行为、写拒绝、结果身份、B&H 成本／覆盖、过期 overlay，以及保存曲线不变，不是完整核心 runtime 回归套件。

在 `code/frontend/pm-console/`：

```sh
npm run typecheck
npm test
```

前端测试覆盖相关页面／组件；DOM 断言不能替代视觉检查或真实模型／provider 运行。

## 扩展检查

新增 feed／provider 检查时间、重复身份、缺失覆盖及下游记录。Schema／prompt 变更检查错误输出与有限修复，也检查正常结果。状态变更检查部分失败和重试。

记录实际执行的检查及覆盖范围，模型、付费 provider 和授权输入检查单独说明，不能把一个通过的 verifier 当作全系统认证。

## 按变更边界选择验证

| 变更 | 有用的验证 |
| --- | --- |
| Evidence mapper | 带时区时间、身份与畸形记录拒绝 |
| Historical reader | 截止前／时／后的 fixture，以及历史版本选择 |
| Agent schema 或 finalizer | 有效输出、拒绝输出与有限修复耗尽 |
| PM／execution flow | 决策 → 执行 → 状态关联，包括延迟／拒绝操作 |
| Saved-results view | PM／Direct 数值不变、禁止写入、拒绝 stale overlay |
| Projection 或前端 | 来源引用、DTO／类型检查与浏览器检查 |

昂贵的完整运行前，先使用小型确定性 fixture 验证 contract。时间测试应独立改变 visibility 和 publication time。执行测试应包含决策未执行的案例，以捕捉把所有建议都当成成交的问题。

Schema validation 检查结构和允许值，业务检查负责引用及阶段含义。测试应覆盖拒绝输出路径，而不只是断言一份示例 JSON 可解析。

报告检查时，区分保存材料算术、确定性运行时测试、UI 测试和真实 provider／模型运行；它们各自证明流程的不同部分。

## 实现依据

- [code/tests](../../../code/tests)
- [code/frontend/pm-console/package.json](../../../code/frontend/pm-console/package.json)
- [code/src/event_trader/contracts](../../../code/src/event_trader/contracts)
- [tools/verify_results.py](../../../tools/verify_results.py)

[文档目录](../index.md) · [English](../../en/development/contracts-and-testing.md)
