# 验证清单

先读 [README.md](README.md) 里的规则。按顺序做，每项完成后写 `results/Vxx.md`、更新 `results/SUMMARY.md`、提交推送。

## 准备（每个新终端都要执行）

```bash
cd <仓库目录>
export REPO="$(pwd)"
export NANOCODE_HOME="$(mktemp -d)"
ncode() { PYTHONPATH="$REPO" python3 -m nanocode "$@"; }
```

真实模型相关的项（V07 起）还需要一个 API Key，放进对应环境变量，例如：

```bash
export OPENROUTER_API_KEY=...     # 或 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ANTHROPIC_API_KEY
export PROVIDER=openrouter        # 与 Key 对应的预设名
export MODEL=<模型名>              # 必须支持工具调用（function calling）
```

**选模型的注意事项**：Agent 依赖工具调用。OpenRouter 的部分免费模型不支持工具调用，`openrouter/free` 也可能路由到这样的模型。
如果 V08 里出现"模型不调用工具"或接口报错与 tools 有关，换一个明确支持工具调用的模型重试，并在结果里写明两次各用了什么模型。

---

## V01 环境、单元测试、评测框架自检

```bash
python3 --version
git rev-parse --short HEAD
python3 -m unittest discover -s tests 2>&1 | tail -3
python3 evals/agent_tasks/run.py --reference --out "$(mktemp -d)" 2>&1 | grep -E "^[✓✗]|通过率"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | Python 版本 | ≥ 3.10 |
| 2 | 单元测试 | 最后显示 `OK`，`Ran N tests` 中 N ≥ 75 |
| 3 | 评测框架自检 | 10 行全是 ✓，`通过率：10/10` |

---

## V02 Mock 模型端到端（命令行）

```bash
W="$(mktemp -d)" && cd "$W"
ncode -p "写一个打招呼的脚本" --mode bypass --trace
ls -A "$W" "$W/nanocode_demo"
python3 "$REPO/verification/trace_stats.py"
printf '你好\ny\ny\n/todos\n/exit\n' | ncode
cd "$REPO"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 单次模式输出 | 包含"全部完成 ✅"和"你好，我是 nanocode！" |
| 2 | 生成文件 | `nanocode_demo/` 下有一个 `hello_*.py` |
| 3 | 工作目录干净 | `ls -A "$W"` **没有** `.nanocode` 目录（会话记录写在 NANOCODE_HOME 里） |
| 4 | trace 汇总 | 工具调用包含 todo_write、glob、write_file、bash；没有"错误："行 |
| 5 | 交互模式 | 出现两次"? 允许 … 吗"（write_file、bash），回答 y 后完成；`/todos` 显示 3 项 ☑ |

---

## V03 Web 界面（浏览器实操）

需要能打开浏览器（人工或浏览器自动化都可以）。

```bash
W="$(mktemp -d)" && cd "$W" && ncode --web --port 8765 &
# 浏览器打开 http://127.0.0.1:8765
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 页面加载 | 顶部显示 `mock/mock-demo` 和工作目录，浏览器控制台没有报错 |
| 2 | 发送消息 | 输入"写一个打招呼脚本"并发送，出现文字流式输出和工具卡片 |
| 3 | 权限确认 | 出现两次"允许 … 吗？"卡片，点"允许"后继续执行 |
| 4 | 循环时间线 | 右侧"循环时间线"有多张卡片，最后一张的 stop_reason 是 `end_turn` |
| 5 | 上下文页签 | 能看到 messages 列表，其中有 role 为 user、内容为 tool_result 的消息 |
| 6 | 系统提示词、工具定义页签 | 有内容，工具定义里有 10 个工具（含 search_docs） |
| 7 | 中断 | 再发一条消息，在出现权限卡片时点"中断"，Agent 停下，页面不卡死 |
| 8 | 学习页 | 打开 http://127.0.0.1:8765/learn，点"下一步"时右侧 messages 增长 |

可以的话截图保存到 `verification/results/assets/V03-*.png`。完成后停止服务：`kill %1`，然后 `cd "$REPO"`。

---

## V04 Web 安全

先按 V03 的方式启动服务（端口 8765），然后：

```bash
B=http://127.0.0.1:8765
code() { curl -s -o /dev/null -w "%{http_code}\n" "$@"; }
code $B/                                                     # 1
code $B/ -H "Host: evil.example:8765"                        # 2
code $B/api/state                                            # 3
TOK=$(curl -s $B/ | grep -o 'const TOKEN = "[^"]*"' | cut -d'"' -f2); echo "令牌长度 ${#TOK}"
code $B/api/state -H "x-nanocode-token: $TOK"                # 4
code -X POST $B/api/chat -H "x-nanocode-token: $TOK" -H "Content-Type: text/plain" -d '{"message":"/cost"}'   # 5
code -X POST $B/api/chat -H "x-nanocode-token: $TOK" -H "Content-Type: application/json" -H "Origin: https://evil.example" -d '{"message":"/cost"}'   # 6
curl -s -X POST $B/api/chat -H "x-nanocode-token: $TOK" -H "Content-Type: application/json" -H "Origin: http://127.0.0.1:8765" -d '{"message":"/cost"}'; echo   # 7
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 正常访问首页 | 200 |
| 2 | DNS 重绑定（伪造 Host）访问首页 | 403 |
| 3 | 不带令牌访问 API | 403 |
| 4 | 带令牌访问 API | 200（令牌长度大于 20） |
| 5 | text/plain 请求（跨站伪造的典型形式） | 415 |
| 6 | 带外部 Origin 的 JSON 请求 | 403 |
| 7 | 正常请求 | `{"ok": true, "output": "输入 0 tokens…"}` |
| 8 | 真实浏览器跨站测试（可选，见下） | Agent 没有被触发 |

检查点 8：在另一个端口起一个"恶意页面"，用真实浏览器打开它，确认 nanocode 没有收到消息。

```bash
E="$(mktemp -d)" && cat > "$E/index.html" <<'HTML'
<script>
fetch("http://127.0.0.1:8765/api/chat", {method: "POST", mode: "no-cors",
  headers: {"Content-Type": "text/plain"}, body: JSON.stringify({message: "写一个打招呼脚本"})});
document.write("已尝试发送跨站请求");
</script>
HTML
(cd "$E" && python3 -m http.server 9000 &)
# 浏览器打开 http://127.0.0.1:9000 ，等几秒后：
curl -s $B/api/state -H "x-nanocode-token: $TOK" | python3 -c "import json,sys; d=json.load(sys.stdin); print('busy', d['busy'], 'usage', d['usage'])"
```

预期：`busy False`，usage 全为 0；nanocode 网页里没有出现这条消息。完成后停止两个服务。

---

## V05 工作区信任

```bash
P="$(mktemp -d)" && mkdir -p "$P/.nanocode" && cd "$P"
cat > .nanocode/settings.json <<'EOF'
{"mcpServers": {"x": {"command": "sh", "args": ["-c", "touch PWNED_BY_MCP; sleep 1"]}},
 "hooks": {"UserPromptSubmit": [{"command": "touch PWNED_BY_HOOK"}]},
 "mode": "bypass"}
EOF
ncode -p "hi" 2>&1 | head -8; ls PWNED* 2>&1          # 第 1 次：不信任
ncode -p "hi" --trust >/dev/null 2>&1; ls PWNED*; rm -f PWNED*            # 第 2 次：--trust
ncode -p "hi" >/dev/null 2>&1; ls PWNED*; rm -f PWNED*                    # 第 3 次：已记住信任
sed -i.bak 's/"bypass"/"accept_edits"/' .nanocode/settings.json         # 改配置
ncode -p "hi" 2>&1 | head -8; ls PWNED* 2>&1          # 第 4 次：配置变了
cd "$REPO"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 第 1 次 | 输出"已忽略"，并列出 `touch PWNED_BY_HOOK`、`touch PWNED_BY_MCP` 两条命令；没有 PWNED 文件 |
| 2 | 第 2 次（--trust） | 两个 PWNED 文件都出现（证明信任后配置会生效） |
| 3 | 第 3 次（不加 --trust） | 两个 PWNED 文件都出现（同一份配置的信任被记住） |
| 4 | 第 4 次（配置被改过） | 再次提示"已忽略"，没有 PWNED 文件 |
| 5 | 交互式询问（可选，需要真实终端） | 在 `$P` 里直接运行 `ncode`，会列出要执行的命令并问"信任这个项目吗？[y/N]"，回答 N 后没有 PWNED 文件 |

---

## V06 路径权限

```bash
python3 - <<EOF
import os, sys, tempfile
sys.path.insert(0, "$REPO")
from nanocode.permissions import PermissionManager
from nanocode.tools import ReadFile, WriteFile, Glob
ws = tempfile.mkdtemp(); os.makedirs(os.path.join(ws, "src"))
os.symlink(os.path.expanduser("~"), os.path.join(ws, "homelink"))
pm = PermissionManager(ws, allow=["write_file(src/*)"], deny=["write_file(secrets/*)"])
c = lambda t, i: pm.check(t, i).behavior
print("1", c(ReadFile(), {"path": "~/.bashrc"}))
print("2", c(ReadFile(), {"path": "homelink/.bashrc"}))
print("3", c(ReadFile(), {"path": "src/a.py"}))
print("4", c(Glob(), {"pattern": "*", "path": ".."}))
print("5", c(WriteFile(), {"path": "src/../../etc/x"}))
print("6", c(WriteFile(), {"path": "src/ok.py"}))
print("7", c(WriteFile(), {"path": "./secrets/k"}))
pm.mode = "accept_edits"
print("8", c(WriteFile(), {"path": "~/.bashrc"}))
EOF
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 读 `~/.bashrc` | ask |
| 2 | 通过软链接读主目录 | ask |
| 3 | 读项目内文件 | allow |
| 4 | glob 项目上级目录 | ask |
| 5 | 用 `..` 绕过 allow 规则 | ask |
| 6 | 命中 allow 规则 | allow |
| 7 | 换种写法绕过 deny 规则 | deny |
| 8 | accept_edits 模式写 `~/.bashrc` | ask |

---

## V07 真实模型：基础对话

```bash
W="$(mktemp -d)" && cd "$W"
ncode --provider "$PROVIDER" --model "$MODEL" -p "用一句话介绍你自己。不要调用任何工具。" --trace; echo "退出码 $?"
python3 "$REPO/verification/trace_stats.py"
cd "$REPO"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 退出码 | 0 |
| 2 | 回答 | 一句通顺的自我介绍（原样粘贴） |
| 3 | token 统计 | 最后一行有 `[tokens 输入 … / 输出 …]`；注明是否带"估算" |
| 4 | trace | 有 llm_request、llm_response、turn_end；没有"错误："行 |

如果有第二家模型的 Key（例如 DeepSeek、千问），用它再做一遍，两次结果都写进报告。

---

## V08 真实模型：修 bug 完整流程

这一项同时验证工具调用、权限规则、hooks 和 MCP。

```bash
T="$(mktemp -d)" && cp -r "$REPO/examples" "$T/" && cd "$T/examples/project"
ncode --provider "$PROVIDER" --model "$MODEL" --trust --mode accept_edits --trace \
   -p "修复 calc.py 里的 bug，让 test_calc.py 全部通过。不要修改测试文件。修完运行 python3 -m unittest test_calc 确认。"
python3 -m unittest test_calc -v 2>&1 | tail -6
diff "$REPO/examples/project/test_calc.py" test_calc.py && echo "测试文件未被修改"
cat .nanocode/edits.log
python3 "$REPO/verification/trace_stats.py"
cd "$REPO"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 启动日志 | 有"MCP 服务器 demo 已连接，提供 2 个工具" |
| 2 | 结果 | 之后手动运行的测试全部通过（4 个） |
| 3 | 测试文件 | `diff` 无输出，打印"测试文件未被修改" |
| 4 | 工具使用 | trace 里有 read_file，以及 edit_file 或 write_file |
| 5 | 先读后改 | 如果出现"修改之前必须先用 read_file 读取"的工具报错，模型随后自行改正（记录是否出现） |
| 6 | 权限规则 | 运行测试的 bash 命令没有被拒绝（命中 allow 规则 `bash(python3 -m unittest*)`）；记录被拒绝的其他命令 |
| 7 | PostToolUse hook | `.nanocode/edits.log` 里有修改记录 |
| 8 | 统计 | 记录模型调用次数、工具调用次数、token |

如果失败，用同一个命令再跑 1~2 次（模型有随机性），每次的结果都记下来。

---

## V09 真实模型：Anthropic 协议（可选）

需要 `ANTHROPIC_API_KEY`，没有就标记 BLOCKED。

```bash
W="$(mktemp -d)" && cd "$W"
ncode --provider anthropic --model <Claude 模型名> -p "用一句话介绍你自己。不要调用任何工具。" --trace; echo "退出码 $?"
python3 "$REPO/verification/trace_stats.py"
cd "$REPO"
```

检查点同 V07。有余力的话，把 V08 也用这个模型跑一遍。

---

## V10 真实模型：search_docs 与子 Agent

```bash
T="$(mktemp -d)" && cp -r "$REPO/docs" "$REPO/README.md" "$T/" && cd "$T"
ncode --provider "$PROVIDER" --model "$MODEL" --trace \
   -p "这个项目的文档里，子 Agent 为什么能保护主上下文？请先用 search_docs 检索，回答时给出出处（文件:行号）。"
python3 "$REPO/verification/trace_stats.py"
ls -A .nanocode/index
ncode --provider "$PROVIDER" --model "$MODEL" --mode bypass --trace \
   -p "请使用 task 工具派一个子 Agent，让它统计 docs 目录下有几个 Markdown 文件、分别是什么，然后根据它的结论回答我。"
python3 "$REPO/verification/trace_stats.py"
ls docs/*.md | wc -l
cd "$REPO"
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 检索工具被使用 | 第一次运行的工具调用里有 search_docs |
| 2 | 出处 | 回答引用了 `docs/06-subagents-todos-skills.md`（带行号更好） |
| 3 | 索引 | `.nanocode/index/` 下有 `docs.json` 和 `.gitignore` |
| 4 | 子 Agent | 第二次运行的 trace 汇总里有 `subagent_start` 和 `subagent_end` |
| 5 | 结论正确 | 回答的文件数与 `ls docs/*.md | wc -l` 一致 |

---

## V11 RAG 向量 / 混合检索评测

需要一个提供 embedding 接口的 Key。任选其一：

| 服务 | 参数 | Key 环境变量 |
|---|---|---|
| 硅基流动 | `--embed-provider siliconflow --embed-model BAAI/bge-m3` | SILICONFLOW_API_KEY |
| 通义千问 | `--embed-provider qwen --embed-model text-embedding-v3` | DASHSCOPE_API_KEY |
| OpenAI | `--embed-provider openai --embed-model text-embedding-3-small` | OPENAI_API_KEY |

（模型名以各家文档为准。OpenRouter 目前不一定提供 embedding 接口。）

```bash
python3 examples/rag_eval/run_eval.py
python3 examples/rag_eval/run_eval.py --embed-provider <服务> --embed-model <模型> --verbose
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 基线 | 第一条命令输出 grep-原句、grep-关键词、bm25 三行（原样粘贴表格） |
| 2 | 向量 | 第二条命令多出 vector、hybrid 两行，没有"向量化失败" |
| 3 | 记录 | 原样粘贴完整表格和逐题结果；写明用的 embedding 服务和模型 |

没有任何 embedding Key 就标记 PARTIAL（只完成检查点 1）。

---

## V12 Agent 任务评测

```bash
python3 evals/agent_tasks/run.py --provider "$PROVIDER" --model "$MODEL" --repeat 3
```

免费模型限流严重时可以先用 `--repeat 1`，并在报告里说明。

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 完整跑完 | 13 个任务都有结果行（T11–T13 是专门设了陷阱的任务，见第 11 章），最后生成 `evals/agent_tasks/results/*.md` 和 `.json` |
| 2 | 记录 | 把生成的 `.md` 汇总表原样贴进 `results/V12.md`；`.md` 和 `.json` 两个文件一起提交 |
| 3 | 失败分析 | 对每个失败的任务，用 `--tasks Txx --keep` 再跑一次，看保留下来的工作目录，写一两句失败原因 |

如果有第二个模型，也跑一遍，方便对比。这一项是简历里最有说服力的数据，请务必如实记录，不要挑好的结果。

---

## V13 真实模型：验证闭环（`--verify`、终审模式、子任务验收、经验候选）

这一项验证第 11 章的功能在真实模型下是否真的有用。开发环境没有 API Key，以上功能目前只在脚本化的模拟模型下测过，**没有任何真实模型的数据**，所以这里的每个结论都要如实记录，包括"没有帮助"。

先做对照实验（T11–T13 的陷阱类型见 `evals/agent_tasks/README.md`）：

```bash
python3 evals/agent_tasks/run.py --provider "$PROVIDER" --model "$MODEL" --tasks T11,T12,T13 --repeat 3
python3 evals/agent_tasks/run.py --provider "$PROVIDER" --model "$MODEL" --tasks T11,T12,T13 --repeat 3 --verify
```

| # | 检查点 | 预期 |
|---|---|---|
| 1 | 对照 | 两组各 9 次运行，分别记录通过数、平均步数、平均 token。`--verify` 组没有更好也要如实写 |
| 2 | 验证闭环 | 在临时项目里用 `ncode --verify "python3 -m unittest" -p "..."` 让模型改一个会让测试失败的地方；trace 里能看到 `verify` 事件，失败时 `-p` 退出码为 1（`echo $?`） |
| 3 | 终审模式 | `ncode --mode review` 让模型改文件并执行 `git push`、`rm -rf`；前者之外的项目内编辑自动放行，后两者必须弹出确认；结束时出现 `review` 事件（Web 面板里能看到 diff） |
| 4 | 子任务验收 | 让模型用 `task` 工具并带上 `check` 和 `allowed_paths`；故意让子任务改范围外的文件，确认系统生成的报告里列出越界文件 |
| 5 | 经验候选 | 一次被验证打回又修好的回合之后，出现 `memory_pending`；`/memory accept 1` 之后 NANOCODE.md 里多一行；`/memory reject all` 能清空 |
| 6 | 记录 | 每个检查点逐个写通过与否；Web 面板（verify / review / memory_pending 的渲染）用浏览器实操，不要只看代码 |

没有 Key 就标记 BLOCKED。检查点 2、3、5 的命令行部分可以用 `--provider mock` 先做一遍，但 mock 的结果不能当作真实模型的结论，要在报告里分开写。
