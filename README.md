回调失败后的重试把同一笔订单推了五次，下游直接重复发货：退避采用固定间隔，重试风暴压垮下游；退避没有上限，等待时间指数爆炸；抖动为零，多个客户端同时重试；去重键只用事件标识不含版本，新版本被当成重复；去重键含时间戳，同一事件每次重试键都不同；计划排序按插入顺序，早该重试的排在后面；投递记录不清理，存储无限增长；重复判定把过期记录也算作重复，正常重试被拒；策略校验不检查退避上限；批量调度复用尝试次数，后一个事件从第一个事件的重试次数开始。engine/hook/retry.py 是核心，tests/hook/test_retry.py 15个红。要修的是：按上面的顺序把 10 个缺陷逐个修掉。

运行方式

在仓库根目录执行 pytest tests -q。当前应看到 15 failed，且没有 error。首轮实现前所有测试都因桩函数抛出 NotImplementedError 而失败，这是预期状态。
目录布局

仓库采用 engine 嵌套布局，engine 是包，engine/hook 是子包，唯一需要修改的实现文件是 engine/hook/retry.py，测试文件是 tests/hook/test_retry.py。目录如下：

sec-webhook-retry/
  README.md
  conftest.py
  engine/__init__.py
  engine/hook/__init__.py
  engine/hook/retry.py
  tests/hook/test_retry.py

测试约束

tests 目录不可修改，评测以 tests/hook/test_retry.py 中的 15 个测试为准。实现只使用标准库，所有时间和抖动系数均显式传入，不使用随机数；数值断言容差为 0。
## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 安全 |
| 子方向 | 漏洞检测与攻防 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

