# 问

你能写一个MD，我要给codex看。看完让他明白我在想要做啥

# 答

# NodeLite：SWE-smith 多任务依赖预扫描与跨 Package Manager 共享缓存设计

## 1. 我想做什么

当前目标是为 **SWE-smith 中大量 JavaScript / TypeScript tasks** 构建一个更轻量、更快的执行环境。

现状是：每个 task 在启动环境时，通常会根据自己的项目依赖执行：

- `npm install` / `npm ci`
- `pnpm install`
- `yarn install`
- `bun install`

如果每个 task 都独立联网下载依赖，会产生大量重复下载、重复缓存和重复安装工作。

我希望把这些工作尽可能提前，并让不同 task、不同 package manager 之间共享已经下载过的 package artifact。

核心想法：

> 在真正运行 tasks 之前，先扫描所有 tasks 的环境配置和 lockfile，知道每个 task 使用什么 package manager、需要哪些精确版本的 package，然后把所有依赖做 union 和去重，提前下载到一个共享的全局 artifact store 中。

---

## 2. 当前选择的 Benchmark

当前决定优先使用：

**SWE-smith**

原因之一是 SWE-smith 的 JS/TS profiles 中已经覆盖多种 Node package manager，包括：

- npm
- pnpm
- Yarn Classic v1
- Yarn Berry v2+
- Bun

因此它适合测试跨 package manager 的依赖共享方案。

---

## 3. Task Scanner 要做什么

不要扫描源码中的 `import`。

Scanner 应该从 **SWE-smith profile / environment definition** 开始，确定每个 task 的：

```text
task
repo
commit
node_version
package_manager
package_manager_version
lockfile
```

例如：

```json
{
  "task": "example-task",
  "repo": "owner/repo",
  "commit": "abc123",
  "node_version": "20",
  "package_manager": "pnpm",
  "package_manager_version": "9.4.0",
  "lockfile": "pnpm-lock.yaml"
}
```

其中：

- `package_manager_version` 需要保留，用于正确解析 lockfile 和复现 task。
- 但是它 **不应该成为底层 package artifact 的存储 key**。

---

## 4. 如何识别 Package Manager

优先级建议：

1. 看 SWE-smith profile / Dockerfile 中真正执行的 install 命令。
2. 看 `package.json` 中的 `packageManager`。
3. 看 lockfile。
4. 看 Yarn / Bun 等特定配置文件。

典型判断：

```text
npm install / npm ci
    -> npm

pnpm install
    -> pnpm

bun install
    -> Bun

yarn install
    -> Yarn
```

Yarn 还需要继续判断版本：

```text
yarn.lock 第一行是:
# yarn lockfile v1
    -> Yarn Classic v1
```

或者：

```text
packageManager: "yarn@4.x"
.yarnrc.yml
.yarn/releases/yarn-4.x.cjs
yarn set version 4.x
    -> Yarn Berry
```

---

## 5. Dependency Scanner / Resolver

在拿到 repo + commit 后：

```text
checkout repo@commit
```

然后读取对应的依赖文件：

| Package Manager | 主要输入 |
|---|---|
| npm | `package-lock.json` + `package.json` |
| pnpm | `pnpm-lock.yaml` + workspace config |
| Yarn v1 | `yarn.lock` + `package.json` |
| Yarn Berry | `yarn.lock` + `.yarnrc.yml` + `package.json` |
| Bun | `bun.lock` / `bun.lockb` + `package.json` |

目标是得到每个 task 的 **精确依赖集合**。

例如：

```json
{
  "task": "task-A",
  "dependencies": [
    {
      "name": "lodash",
      "version": "4.17.21",
      "source": "npm-registry",
      "resolved": "...",
      "integrity": "sha512-..."
    },
    {
      "name": "react",
      "version": "18.3.1",
      "source": "npm-registry",
      "resolved": "...",
      "integrity": "sha512-..."
    }
  ]
}
```

注意：

不要自己简单根据 `package.json` 中的：

```json
"foo": "^1.0.0"
```

猜最终版本。

应该尽量依赖 lockfile，因为 lockfile 才能给出精确版本和完整 transitive dependency graph。

---

## 6. 不要自己重写五套 Dependency Resolver

不要第一版就自己实现：

```text
parse_package_lock()
parse_pnpm_lock()
parse_yarn_v1_lock()
parse_yarn_berry_lock()
parse_bun_lock()
```

因为实际还需要处理：

```text
peerDependencies
optionalDependencies
workspaces
overrides
resolutions
aliases
git dependencies
file dependencies
workspace protocol
patches
OS / CPU constraints
```

更稳妥的方向是：

> Scanner 负责识别 task、package manager、lockfile；真正的 dependency resolution 尽量调用原生 package manager 或兼容解析器完成。

例如：

```text
npm task  -> npm resolver
pnpm task -> pnpm resolver
yarn task -> yarn resolver
bun task  -> bun resolver
```

然后把解析结果转换成 NodeLite 自己统一的 Dependency Manifest。

---

## 7. 多个 Task 做 Global Union

假设：

```text
Task A
  lodash@4.17.21
  react@18.3.1
  axios@1.7.9

Task B
  lodash@4.17.21
  typescript@5.7.3

Task C
  react@18.3.1
  typescript@5.7.3
```

聚合之后：

```text
Global Manifest

lodash@4.17.21
react@18.3.1
axios@1.7.9
typescript@5.7.3
```

也就是说：

> 同一个 package artifact 即使被几千个 task 使用，也只需要在全局 store 中准备一次。

---

## 8. npm / pnpm / Yarn / Bun 下载的是不是同一个东西

如果四个 package manager 最终都需要：

```text
lodash@4.17.21
```

并且来自同一个 npm registry，那么它们请求的原始 package artifact 通常是同一份 tarball，例如：

```text
lodash-4.17.21.tgz
```

因此可以跨 package manager 共享 **原始下载 artifact**。

推荐不要使用：

```text
npm/lodash@4.17.21
pnpm/lodash@4.17.21
yarn/lodash@4.17.21
```

而应该使用内容 hash：

```text
sha512-xxxxxxxx
    -> lodash-4.17.21.tgz
```

也就是说，底层 store 最好是：

**Content Addressed Storage, CAS**

---

## 9. Package Manager Version 的作用

例如：

```json
"package_manager": "pnpm",
"package_manager_version": "9.4.0"
```

不同 PM 版本可能影响：

- lockfile 能否读取
- dependency resolution
- peer dependency 行为
- optional dependency 行为
- workspace 行为
- lockfile format

所以必须记录 PM version。

但是，一旦已经确定最终需要：

```text
lodash@4.17.21
```

那么其原始 tarball 通常和：

```text
npm 9
npm 10
pnpm 8
pnpm 9
Yarn 1
Yarn 4
Bun
```

无关。

因此：

```text
package_manager_version
```

属于 **Task / Resolution Metadata**，

而不属于 **Raw Artifact CAS key**。

---

## 10. 当前 Package Manager 的 Cache 并不统一

虽然它们下载的原始 `.tgz` 可以是同一个，但下载之后各自会保存成不同的 cache/store 格式。

大致可以理解成：

```text
                 lodash-4.17.21.tgz
                           |
        ---------------------------------------
        |              |          |           |
       npm            pnpm       Yarn         Bun
        |              |          |            |
    npm cache       pnpm store   Yarn cache   Bun cache
```

这些 cache 不应该直接强行指向同一个目录。

原因是：

```text
npm 不认识 pnpm store
pnpm 不认识 Yarn cache
Yarn 不认识 Bun cache
```

---

## 11. 当前推荐的两级缓存设计

我目前更倾向于：

### G0：跨 Package Manager 统一的 Raw Artifact CAS

永久保存：

```text
Global Raw Artifact CAS
├── sha512-A
├── sha512-B
├── sha512-C
└── ...
```

metadata 可以维护：

```text
lodash@4.17.21
    -> sha512-A
    -> source URL
    -> integrity
```

这一层：

- npm 共用
- pnpm 共用
- Yarn 共用
- Bun 共用
- 所有 task 共用

这是最重要的跨 PM 去重层。

---

### G1：每种 Package Manager 自己的共享 Native Cache

例如：

```text
shared npm cache
shared pnpm store
shared Yarn cache
shared Bun cache
```

同一种 package manager 的所有 tasks 共用同一个 native cache。

例如：

```text
Task A npm ----\
Task B npm -----+--> shared npm cache
Task C npm ----/
```

而不是每个 task 一个 cache。

整体：

```text
                       Internet
                          |
                          v
                 Global Raw CAS
                  (one artifact)
                          |
        ---------------------------------------
        |              |          |           |
        v              v          v           v
    npm cache      pnpm store   Yarn cache   Bun cache
        |              |          |           |
     npm tasks       pnpm tasks  Yarn tasks   Bun tasks
```

---

## 12. 为什么不直接只保留四种 Native Cache

因为如果三个 task：

```text
Task A -> npm
Task B -> pnpm
Task C -> Yarn
```

都需要：

```text
foo@1.0.0
```

那么原始网络 artifact 可能只是一份：

```text
foo-1.0.0.tgz
```

但 native cache 可能分别存：

```text
npm cache       一份
pnpm store      一份
Yarn cache      一份
```

也就是说同一个逻辑 package 会存在多个物理表示。

所以 G0 Raw CAS 的作用是：

> 至少保证 Internet -> Local 的下载只发生一次，并保留一个跨 PM 的唯一源 artifact。

---

## 13. Cache 大小的问题

不要用：

```text
task 数量 × node_modules 大小
```

估算。

真正应该统计：

```text
所有 task dependency references
            |
            v
(name, version, integrity)
            |
          dedup
            |
            v
Unique Package Artifacts
```

建议 Scanner 最终统计：

```text
# tasks
# dependency references
# unique packages
# unique package versions
# unique artifact hashes

raw download bytes
deduplicated raw bytes
native cache bytes for npm
native cache bytes for pnpm
native cache bytes for Yarn
native cache bytes for Bun

dedup ratio
saved network traffic
```

这些都可以成为后续 benchmark 指标。

---

## 14. 能否提前加载

可以，而且这是核心设计。

流程：

```text
所有 SWE-smith Tasks
        |
        v
1. Task Scan
        |
        v
repo / commit / node / PM / PM version
        |
        v
2. Dependency Resolution
        |
        v
per-task dependency manifest
        |
        v
3. Global Union + Dedup
        |
        v
global dependency manifest
        |
        v
4. Prefetch
        |
        v
Global Raw Artifact CAS
        |
        v
5. Warm Native Caches
        |
        +--> npm cache
        +--> pnpm store
        +--> Yarn cache
        +--> Bun cache
        |
        v
6. 正式运行 Tasks
        |
        v
offline / local-only install
```

---

## 15. Prefetch 和 Warm Cache 要分开

### Prefetch

目标：

```text
Internet
    ->
Global Raw Artifact CAS
```

每个 unique artifact 最多下载一次。

### Warm Native Cache

目标：

```text
Global Raw CAS
    ->
npm cache / pnpm store / Yarn cache / Bun cache
```

把 package manager 自己需要的格式提前准备好。

这两步应该是两个独立阶段。

第一版甚至可以只实现 Prefetch。

---

## 16. Local Registry Proxy 的作用

可以在 package manager 和 Global Raw CAS 中间提供一个 npm-compatible registry proxy。

例如：

```text
npm
pnpm
Yarn
Bun
 |
 v
Local Registry Proxy
 |
 v
Global Raw CAS
 |
 v
registry.npmjs.org
```

逻辑：

```text
PM 请求 lodash@4.17.21

Local Registry:
    if artifact exists in Global CAS:
        return local artifact
    else:
        fetch from upstream registry
        store in Global CAS
        return artifact
```

因此 package manager 不需要知道 NodeLite 的 CAS 格式。

它仍然认为自己在访问普通 npm registry。

---

## 17. Static Scan 无法覆盖所有下载

lockfile 能覆盖大量 npm registry dependencies，但不能保证覆盖安装期间的一切网络请求。

例如某些 package 的：

```text
postinstall
```

可能会额外下载：

```text
native binary
Chromium
Playwright browser
Puppeteer browser
prebuilt addon
GitHub release artifact
```

因此最终可以分两阶段：

### Stage A：Static Dependency Prefetch

从 lockfile 解析 npm package artifacts。

### Stage B：Warm-up Install / Download Capture

第一次实际 install 时允许网络，并记录所有额外下载：

```text
npm registry
GitHub
browser CDN
binary CDN
...
```

然后也把这些 artifact 加入共享 store。

后续 task 可以完全 offline 或接近 offline。

---

## 18. 第一版不要做什么

第一版暂时不要：

1. 重写 npm/pnpm/Yarn/Bun dependency resolver。
2. 强制四个 package manager 直接共用一种 native cache 格式。
3. 自己重新实现 `node_modules` layout。
4. 统一 npm/pnpm/Yarn/Bun 的 hoisting / symlink / PnP 语义。
5. 提前解决所有 native addon / postinstall / browser artifact。

第一版优先验证：

> 跨 task、跨 package manager 的 dependency artifact 是否可以通过预扫描、全局去重和提前下载显著降低环境准备时间和网络流量。

---

## 19. 建议第一版实现范围

### Phase 1：Scanner

输入：

```text
SWE-smith profiles
```

输出：

```json
{
  "task": "...",
  "repo": "...",
  "commit": "...",
  "node": "...",
  "package_manager": "...",
  "package_manager_version": "...",
  "lockfile": "..."
}
```

---

### Phase 2：Dependency Manifest

每个 task 输出：

```json
{
  "task": "...",
  "dependencies": [
    {
      "name": "...",
      "version": "...",
      "source": "...",
      "integrity": "..."
    }
  ]
}
```

---

### Phase 3：Global Aggregator

输入：

```text
task_1.json
task_2.json
...
task_n.json
```

输出：

```text
global_manifest.json
```

按：

```text
integrity hash
```

去重。

---

### Phase 4：Global Raw Artifact CAS

实现：

```text
artifact exists?
    yes -> reuse
    no  -> download once
```

目录可以类似：

```text
store/
  sha512/
    ab/
      abcdef...
```

另有 metadata index：

```text
package + version + source
        ->
integrity hash
```

---

### Phase 5：验证

选几个使用不同 PM 的 SWE-smith repo：

```text
npm
pnpm
Yarn Classic
Yarn Berry
Bun
```

让它们依赖一部分相同的 npm packages。

验证：

1. 第一次运行会下载。
2. 后续不同 task 不重复下载。
3. 不同 PM 可以复用相同 raw artifact。
4. offline install 仍然可以完成。
5. dependency tree 和原 SWE-smith 环境一致。
6. test command 结果一致。

---

## 20. 我当前最想验证的问题

Codex 在实现前，请优先确认下面这些事实和工程边界：

```text
A. SWE-smith profile 中如何稳定提取：
   repo
   commit
   node version
   package manager
   package manager version

B. 每种 PM 如何在“不真正完成整个 install”的情况下，
   尽可能可靠地得到完整 dependency artifact list。

C. npm / pnpm / Yarn / Bun 是否都可以配置到同一个
   npm-compatible local registry。

D. 如何让 Local Registry / CAS 返回已有 tarball，
   而不让 PM 再访问公网。

E. 是否能在不修改 package manager 源码的情况下，
   完成 cross-PM raw artifact sharing。

F. 对 git / file / workspace / patch / optional / native
   dependencies 应该如何处理。

G. 如何验证预加载后的依赖环境与原 SWE-smith Dockerfile
   行为完全一致。
```

---

## 21. 最终目标一句话

> **在 SWE-smith 执行之前，对整个 workload 做依赖规划，将所有 task 的 Node package dependencies 预解析、去重并提前下载到一个跨 npm/pnpm/Yarn/Bun 共享的全局 artifact store；正式运行时尽可能只做本地 cache hit 和依赖视图构建，不再重复联网下载。**

当前第一优先级不是统一所有 package manager 的内部实现，而是：

> **先统一下载层，再逐步研究是否值得统一 native cache / package store 层。**
