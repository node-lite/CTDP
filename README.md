# NodeLite 依赖准备工具

这个项目用于为固定的 64 个 SWE-smith JavaScript/TypeScript 项目准备依赖。
它会读取 `swe_smith_64_project_ids.txt`，查找每个项目的准确提交和环境，识别 npm、pnpm、Yarn Classic、Yarn Berry、Bun，解析并归一化依赖，然后把可下载的依赖保存到全局 CAS（内容寻址存储）中。

项目不会自己重写包管理器的解析算法，而是调用真实的 npm、pnpm、Yarn 或 Bun。Yarn Classic v1 和 Yarn Berry v2+ 会分别处理。

## 快速运行

仓库自带 `nodelite-deps` 启动脚本，不需要先安装 Python 包：

```bash
./nodelite-deps discover --ids swe_smith_64_project_ids.txt --out acceptance-out
./nodelite-deps resolve --out acceptance-out
./nodelite-deps normalize --out acceptance-out
./nodelite-deps aggregate --out acceptance-out
./nodelite-deps prefetch --out acceptance-out --jobs 16
./nodelite-deps warm-cache --out acceptance-out
./nodelite-deps validate --out acceptance-out
```

也可以一次运行全部阶段：

```bash
./nodelite-deps all --ids swe_smith_64_project_ids.txt --out acceptance-out --jobs 16
```

每个阶段都支持 `--force` 和 `--timeout`，会把结构化日志写入 `out/logs/`，把运行状态和 fingerprint 写入 `out/state/`。输入没有变化且上次成功时，阶段会自动复用已有结果。

## 输出内容

- `out/projects/`：每个项目的发现结果、源码快照、lockfile、解析结果和日志。
- `out/global/`：所有项目合并后的依赖清单和 artifact 索引。
- `out/cas/`：按内容哈希保存的全局依赖文件。
- `out/native-cache/`：由真实包管理器生成的 npm、pnpm、Yarn、Bun 原生缓存。
- `out/reports/summary.md`：人类可读的汇总报告。
- `out/reports/summary.json`：机器可读的汇总数据。
- `out/reports/projects.csv`：项目级结果。
- `out/reports/resolution.csv`：依赖 root 和 lockfile 解析结果。
- `out/reports/artifacts.csv`：依赖 artifact、来源、完整性和 CAS 状态。
- `out/reports/dedup.json`：全局去重统计。
- `out/reports/failures.json`：解析、预取、缓存和验证失败。
- `out/reports/manual_review.json`：需要人工检查的协议或项目。

## 处理流程

1. **发现环境**：取得官方 SWE-smith profile、完整 commit、Node 版本、包管理器、版本、安装目录和命令。
2. **解析 lockfile**：判断现有 lockfile 是否可信；需要时在临时目录调用真实包管理器重新解析。
3. **归一化依赖**：统一处理 registry、Git、HTTP tarball、workspace、local file、patch 和 unknown 类型。
4. **全局去重**：不同项目或不同包管理器引用同一不可变 artifact 时，只保存一份。
5. **预取到 CAS**：校验 SHA-256/SRI，使用原子写入，并支持并发请求合并。
6. **生成原生缓存**：通过真实包管理器把 CAS 中的文件导入各自的缓存格式，不手写缓存内部结构。
7. **动态验证**：使用本地 registry 执行安装，记录额外的外部网络请求和安装失败。

## 当前环境说明

解析和验证会优先使用系统中已经安装的真实包管理器。缺少 pnpm、Yarn、Bun、Corepack 或 Docker 时，工具会明确记录失败或 partial 状态，不会把它们假装成成功。

lockfile 中的 `workspace:`、`link:`、`file:`、patch 和未知协议不会被错误地当成普通 registry 下载；这类内容会保留并列入人工检查清单。

动态验证还可能发现 lockfile 没有记录的下载，例如 Electron、Playwright、浏览器驱动、预编译二进制或安装脚本中的 Git 下载。这些结果会单独标记为 `external_artifact_miss`。

## 测试

运行单元测试和小型集成测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

固定 64 个 profile 的详细结果见 `acceptance-out/reports/summary.md`。
