# 供应链金融平台



## 运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --host 127.0.0.1 --port 8002
```

打开 `http://127.0.0.1:8002`。接口文档位于 `http://127.0.0.1:8002/docs`。

页面右上角可切换角色和当前机构，以演示 RBAC 权限：

- 供应商“华星电子”：登记账款、对已确权账款申请融资；
- 核心企业“远航科技”：确认或驳回本企业相关账款；
- 金融机构“海川银行”：审批融资并设置利率；
- 监管机构：查询完整链上事件轨迹；
- 管理员：用于课堂演示的全权限角色。

项目使用 SQLite 保存链下业务明细，通过追加式 `chain_events` 账本模拟联盟链交易、区块高度、交易哈希和业务载荷哈希。接入真实联盟链时，可将 `chain_write()` 替换为 Web3.py 或联盟链 SDK 调用，业务层无需重写。
