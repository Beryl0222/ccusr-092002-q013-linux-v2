"""JSON 文件存储。

单文件、线程安全、原子替换落盘。所有时间字段以 ISO-8601 字符串保存，
读取后由业务层用 clock.parse 解释。默认内存模式（path=None）便于测试。
"""

import json
import os
import tempfile
import threading


def blank_data():
    return {
        "schema": 1,
        "hospitals": {},          # hospital_id -> 医院
        "staff": {},              # hospital_id -> [人员资质]
        "equipment": {},          # hospital_id -> [设备点检]
        "channels": {},           # hospital_id -> 绿色通道点检
        "availability": {},       # hospital_id -> [可承诺时段]
        "drills": {},             # drill_id -> 演练记录（含派发时资源快照）
        "events": {},             # drill_id -> [因果事件日志]
        "pending_events": {},     # drill_id -> [前驱未到的离线事件]
        "graders": {},            # grader_id -> 评委（含利益关系）
        "rubrics": [],            # 不可变评分规则版本（按生效期）
        "gradings": {},           # drill_id -> [评分版本（初评/复核）]
        "appeals": {},            # drill_id -> [申诉]
        "certificates": {},       # hospital_id -> {current, ledger}
        "attendance": {},         # hospital_id -> [出席/缺席流水]
        "sequences": {"drill_dispatch": 0},
    }


class Store:
    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.data = blank_data()
        if path and os.path.exists(path):
            self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 向前兼容：补齐缺失的顶层集合
        merged = blank_data()
        merged.update(data)
        self.data = merged

    def save(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".drillcert-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- 便捷访问器 -------------------------------------------------------

    def get_hospital(self, hospital_id):
        return self.data["hospitals"].get(hospital_id)

    def require_hospital(self, hospital_id):
        from .errors import NotFoundError

        hospital = self.get_hospital(hospital_id)
        if hospital is None:
            raise NotFoundError(f"医院不存在：{hospital_id}")
        return hospital

    def list_for(self, key, hospital_id):
        return self.data[key].setdefault(hospital_id, [])

    def next_seq(self, name):
        seq = self.data["sequences"].get(name, 0) + 1
        self.data["sequences"][name] = seq
        return seq
