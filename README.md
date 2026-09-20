# 断肢救治协同计时

在黄金救治窗口内匹配显微外科能力、运输方案和接诊承诺。

`contracts/trauma_alert.json` 保存公开的领域样例，用来约定外部数据的名称与层级；样例不含真实个人资料、业务凭据或生产连接信息。

执行 `python3 service.py --check` 可检查服务身份，运行 `python3 -m unittest discover -s tests -v` 可核对基础契约。服务启动后，`/health` 返回项目标识。
