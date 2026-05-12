# @purpose: 应用入口，启动 uvicorn 装载 FastAPI 应用
# @layer: adapter
# @contract:
#   - app: FastAPI 实例 (供 uvicorn main:app 引用)
# @depends:
#   - uvicorn
#   - ./api/app.py
# @invariants:
#   - 仅做应用装配与启动，不含业务逻辑
#   - 默认监听 127.0.0.1:8000，仅本机调试

import uvicorn
from api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
