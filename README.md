# 显微外科能力演练与认证服务

在**不接入真实患者调度**的前提下，用盲演练检验区域内医院的显微外科接诊能力，
并把能力认证、暂停、整改复测和覆盖空白管理起来。任何模拟数据都**不得写回临床
病历**。

`contracts/trauma_alert.json` 只保存公开的领域样例，用来约定外部数据的名称与
层级（`injury_at` / `preservation` / `injuries[].vessel_mm`）；服务只取其字段
结构，不读取也不生成真实患者数据。

## 红线：临床隔离

- 演练编号独占 `DRILL-9999-XXXX` 号段，与真实告警 `TRAUMA-*` 严格隔离；载荷中
  出现真实告警号、病历/EHR/患者标识字段一律拒绝（`clinical_isolation_violation`）。
- 所有响应带 `X-Simulation-Only: 1`、`X-Clinical-Writeback: forbidden` 头与中文
  免责声明，防止下游误把演练当调度指令。

## 领域流程

1. **资源预维护**：医院登记人员资质（有效期）、显微设备点检（下次点检期）、绿色
   通道状态和可承诺时段。
2. **盲演练派发**：按受伤时间（工作日白天/晚间/深夜/周末）×保存条件（冷藏干燥、
   冷冻、消毒液浸泡等）×血管口径（≤0.8mm 超级显微/手指/肢体）组合生成情境；
   评分要点对参演医院不可见（医院视图只拿到情境与时限）。派发瞬间冻结**资源快照**。
3. **限时响应**：医院提交接诊判断（accept/transfer/decline）、资源锁定证据（带
   sha256）和备选方案。
   - 截止时间锚定派发时刻（白天/晚间 30 分钟，深夜 45 分钟）；
   - 按 `submission_id` 幂等去重，**重复提交不刷新响应时钟**；
   - 断网回执带因果前驱 `after`，重连后按因果合并、去重，前驱未到先挂起、到齐
     级联释放，最终链为稳定拓扑序。
4. **评分**：专家组发布**带生效期的不可变规则版本**（生效区间不重叠，按派发时刻
   选版，不溯及既往）；评委登记利益关系，命中被评医院直接回避（403）。评分按
   派发快照核验资源，含自动项（时限/判断/锁定证据/保存风险识别）与人工项。
5. **申诉**：只追加不可变复核版本（v2、v3…），初评原样保留；复核评委同样回避。
6. **认证**：通过即签发两年期证书。证书到期、连续缺席 2 场、关键资源（显微外科
   医师/显微镜/显微器械/绿色通道）当前失效，都会暂停对外能力标识；整改后通过一场
   新演练复测才换发新证（ledger 保留旧证链）。
7. **覆盖空白**：`GET /coverage` 给出各地区 4 时段 × 3 血管档 = 12 格的覆盖矩阵，
   并标注是否演练过不当保存。
8. **全链追溯**：`GET /certificates/{cert_no}/trace` 从证书追到原始演练、派发时
   资源快照、提交与证据、事件链、评分依据版本、申诉和整改复测。

## 运行

```bash
python3 service.py --check                              # 身份与隔离自检
python3 service.py --port 8000 --data data/drill.json   # 启动（默认纯内存）
python3 -m unittest discover -s tests -v                # 56 个用例
```

## HTTP 接口（均返回 JSON，写接口需 JSON 对象）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/hospitals` | 登记医院 |
| PUT/GET | `/hospitals/{hid}/staff` | 维护/查看人员资质 |
| PUT/GET | `/hospitals/{hid}/equipment` | 维护/查看设备点检 |
| PUT | `/hospitals/{hid}/green-channel` | 绿色通道开关 |
| POST | `/hospitals/{hid}/availability` | 新增可承诺时段（不可重叠） |
| POST | `/hospitals/{hid}/drills` | 派发盲演练（`overrides` 指定三维；`use_contract_sample:true` 对齐合同样例） |
| GET | `/hospitals/{hid}/capability` | 对外能力标识与暂停原因 |
| GET | `/drills/{did}` | 演练的医院视图（无评分要点） |
| POST | `/drills/{did}/response` | 提交响应（头 `X-Hospital-Id`；可带 `offline_receipts`） |
| POST | `/drills/{did}/receipts` | 网络恢复后补送断网回执 |
| POST | `/drills/{did}/grade` | 专家初评 |
| POST | `/drills/{did}/appeal` | 医院申诉（头 `X-Hospital-Id`） |
| POST | `/reviews` | 申诉复核（追加版本） |
| POST | `/graders` | 登记评委及利益关系 |
| POST | `/rubrics` | 发布不可变评分规则版本 |
| POST | `/admin/reconcile` | 结算缺席/暂停（也可由查询隐式触发） |
| GET | `/coverage` | 地区 × 伤情覆盖空白矩阵 |
| GET | `/certificates/{cert_no}/trace` | 证书全链追溯 |
| GET | `/health` | 服务身份 |

## 代码结构

```
drillcert/
  firewall.py     临床隔离：号段、字段扫描、模拟标记
  scenario.py     三维盲演练生成（纯函数、种子可复现）
  resources.py    医院/人员/设备/通道/时段维护与派发快照
  drills.py       派发、幂等提交、截止时钟、离线回执因果合并
  grading.py      规则版本、评委回避、自动评分、申诉复核版本
  certificates.py 证书签发/暂停/复测、覆盖矩阵、追溯
  store.py        线程安全 JSON 存储（原子落盘）
  service.py      门面编排；api.py 为 HTTP 路由
```
