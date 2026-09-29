# MovieClaw 公开演示站

一台给访客（以及 App Store 审核）随便点的 MovieClaw：登录页直接公布账号密码，
超管和几种成员角色都能登录体验，但**全站只读**——改密码、增删成员、删片、改设置、
接 PT 站点都会被服务端拒绝；资源站搜索不开放。「发现」照常能逛，订阅面板
也能打开，只有确认订阅时提示「演示站不会真的订阅和下载」。AI 助手里有几段预置对话，
继续聊时由预设回复的「演示模型」作答（不接真实大模型），会话按登录设备隔离。媒体库里只有开放授权的内容：
13 部 Blender 开放电影（CC BY）和 58 张 Wikimedia Commons 精选图片（CC0）。

设计与安全模型见 [docs/design/demo-site.md](../docs/design/demo-site.md)，
内容署名见 [CREDITS.md](CREDITS.md)。

## 目录里有什么

| 文件 | 作用 |
|---|---|
| `accounts.json` | 演示账号：登录页公布的 4 个账号 + 建站用的成员角色配置 |
| `content.json` | 内容清单：影片来源、授权、TMDB 编号、中文简介；图片的 Commons 出处 |
| `content.lock.json` | 来源指纹：首次下载时生成，之后任何机器重下都必须一致 |
| `fetch_content.py` | 下载、校验、整理成媒体库目录（在镜像里跑，自带 ffmpeg 与 Pillow） |
| `provision.py` | 建站：超管、媒体库、成员角色、精选合集，最后逐个验证账号能登录 |
| `docker-compose.yml` | 演示站 + Caddy（自动 HTTPS） |
| `Caddyfile` | HTTPS 反代配置 |
| `reset.sh` | 黄金快照（`snapshot`）与每日还原（`restore`），都会以演示模式重启并自检 |
| `CREDITS.md` | 署名清单（由 `fetch_content.py --credits` 生成） |

## 演示账号

| 账号 | 密码 | 角色 | 能看到什么 |
|---|---|---|---|
| admin | movieclaw | 超级管理员 | 全部管理功能（只读） |
| family | movieclaw | 家庭成员 | 全部媒体库，能点「订阅」（演示站不会真的下载） |
| kids | movieclaw | 小朋友 | 只有「动画短片」「图片」，分级 ≤ 7 岁，没有订阅入口 |
| guest | movieclaw | 朋友 | 只有「电影」，没有订阅入口 |

另有一个已停用的成员 `former`，只为让成员管理页有「停用」状态可看，不能登录。
改账号只改 `accounts.json`，然后重新建站（见下文「改内容 / 改账号」）。

> **安全红线：公网上绝不能出现非演示模式的实例。** 超管密码是公开的，非演示模式下
> 谁都能用它登录一个没有只读守卫的超管；还没初始化的实例更糟，任何人都能抢先注册
> 超管。建站（第 4 步）必须先 `docker compose down` 停掉 Caddy，只起 movieclaw；
> 之后一律用 `./reset.sh` 启动服务，它会确认处于演示模式后才启动 Caddy。

## 部署

服务器上的演示站目录（例如 `/srv/movieclaw-demo`）就是本目录的一份拷贝，
`data/`、`media/` 会建在它旁边。以下命令都在这个目录里执行，需要 root。
服务器建议 2 核 / 4 GB / 40 GB 磁盘起步，带宽越大越好（影片码率 2～6 Mbps）。

**前提**

- Docker 与 Compose v2 插件（`docker compose`，不是老的 `docker-compose`），
  版本 2.3 以上（`docker-compose.yml` 顶层的 `name:` 需要它；`docker compose version`
  查看）。Debian / Ubuntu 用 Docker 官方脚本一次装齐：

  ```bash
  curl -fsSL https://get.docker.com | sh
  docker compose version
  ```

- `curl`（`reset.sh` 启动后自检用；没有 curl 时用 wget 也行）。
- 服务器要能访问 TMDB（`api.themoviedb.org` 与 `image.tmdb.org`）：建站时靠它识别影片、
  下载海报，演示站运行时「发现」页也要用。

**磁盘怎么算**：内容约 2.5 GB，下载时下载缓存与整理好的媒体目录同时存在，峰值约
2 倍（5 GB 左右，跑完删掉 `.demo-cache` 就回到 2.5 GB）；每日还原时快照包、新解出的
`data/` 与旧 `data/` 同时存在，约占 `data/` 的 3 倍（建站后用 `du -sh data` 看一眼）；
再加上镜像本身几个 GB。40 GB 的盘足够，至少留出 20 GB 空闲。

### 1. 构建镜像（在开发机上）

演示站必须用本分支（`feat/demo`）构建的镜像：官方镜像没有演示模式，也没有
iOS App 需要的设备登录接口。

```bash
# TMDB Key 必填：构建脚本读环境变量 TMDB_API_KEY（或仓库根目录 .env 里的同名项），
# 找不到就退出。服务器是 x86_64 就加 PLATFORM；Apple Silicon 上交叉构建会慢一些
TMDB_API_KEY=你的key TAG=demo-$(git rev-parse --short HEAD) PLATFORM=linux/amd64 \
    ./scripts/build-image.sh
docker save movieclaw:demo-<sha> | gzip | ssh root@<VPS> 'gunzip | docker load'
```

> TMDB Key 以环境变量（`ENV`）的形式写进了镜像，`docker inspect` 就能看到。
> **带 Key 的镜像不要推到公开的镜像仓库**，按上面的 `docker save | docker load` 直接传到服务器。

### 2. 准备目录

```bash
# 在开发机上：把本目录拷到服务器
rsync -av demo/ root@<VPS>:/srv/movieclaw-demo/

# 在服务器上
cd /srv/movieclaw-demo
cat > .env <<'EOF'
DEMO_DOMAIN=demo.example.com
MOVIECLAW_DEMO_IMAGE=movieclaw:demo-<sha>
EOF
```

域名的 A 记录先指到这台服务器，80/443 端口放行（Caddy 要用它们申请证书）。

### 3. 下载内容（约 2.5 GB，只需一次）

```bash
source .env
docker run --rm --entrypoint python -v "$PWD:/work" -w /work "$MOVIECLAW_DEMO_IMAGE" \
    fetch_content.py --out /work/media --cache /work/.demo-cache
```

- 来源只有 Blender 官方下载站、Wikimedia Commons 与 Internet Archive；
- 图片下载前会重新向 Commons 核对授权，不是 CC0 就中止；
- 每个来源文件都按 `content.lock.json` 比对指纹，不一致就中止；
- 首次运行若生成了新的指纹，把 `content.lock.json` 拷回仓库提交；
- 跑完可以删掉 `.demo-cache`。

### 4. 建站（演示模式关闭，Caddy 必须停着）

```bash
# 先停掉全部服务，尤其是 Caddy：接下来的实例不在演示模式，绝不能暴露在公网
docker compose down
# 只起 movieclaw（它只监听 127.0.0.1:3000，没有 Caddy 就进不来公网）
MOVIECLAW_DEMO_MODE=false docker compose up -d movieclaw
python3 provision.py --server http://127.0.0.1:3000 --media-root /media
# 服务器上没有 python3 时，借镜像里的：
# docker run --rm --network host --entrypoint python -v "$PWD:/work" -w /work \
#     "$MOVIECLAW_DEMO_IMAGE" provision.py --server http://127.0.0.1:3000 --media-root /media
```

脚本会等扫描、TMDB 刮削、缩略图与进度条预览都生成完（十来分钟），期间可以重跑，
已完成的步骤会跳过。最后应当看到每个库「已识别 N / 期望 N」、4 个账号都能登录和
「建站完成」。

- 任何一个库的条目数与清单不符，脚本会报错退出（退出码非 0）：**这时不要打快照**，
  按提示修好（多半是 TMDB 访问不了或媒体目录没准备好）后重跑；
- 在宿主机上跑时，脚本发现 Caddy 容器在运行会直接拒绝执行（借镜像跑时查不了，
  只能靠上面的 `docker compose down`）。

> **在第 5 步完成之前不要启动 Caddy、不要执行不带参数的 `docker compose up -d`**：
> 这时的实例不在演示模式，一旦暴露在公网，任何人都能抢注超管或用公开密码登录。

### 5. 打黄金快照，打开演示模式

```bash
./reset.sh snapshot
```

它会：停 movieclaw → 把 `data/` 打包成 `golden-data.tar.gz` → 以
`MOVIECLAW_DEMO_MODE=true` 重新创建并启动 movieclaw → 自检（轮询
`http://127.0.0.1:3000/api/v1/auth/bootstrap`，要求已初始化、且带演示账号）→
通过后才启动 Caddy。自检 180 秒内没通过，就把 movieclaw 与 Caddy 都停掉并报错退出，
按提示看 `docker compose logs --tail 100 movieclaw` 排查。

之后需要手动重启服务时也用 `./reset.sh restore`（顺带还原一次），不要用
`docker compose start`：它沿用容器创建时的环境，若容器是建站时以演示模式关闭
创建的，拉起来的就是非演示实例。

### 6. 每天还原

```bash
crontab -e
# 每天 4:17 还原到黄金快照
17 4 * * * cd /srv/movieclaw-demo && ./reset.sh restore >> reset.log 2>&1
```

cron 按**宿主机的时区**计时，而很多 VPS 默认是 UTC（北京时间 4:17 = UTC 20:17）。
用 `timedatectl` 查看，要按北京时间就先 `timedatectl set-timezone Asia/Shanghai`
（改完重启 cron 服务，Debian / Ubuntu 上是 `systemctl restart cron`），或者直接按 UTC
换算写时间。CentOS / Fedora 的 cronie 也可以在 crontab 第一行写
`CRON_TZ=Asia/Shanghai`；Debian / Ubuntu 自带的 cron 不一定认这个变量，别依赖它。

还原失败（解包、换目录出错，或启动后自检没通过）时 `reset.log` 里有中文说明：
中途出错会把服务按演示模式重新拉起来；自检没通过则 movieclaw 与 Caddy 都是停着的，
演示站暂时打不开，但不会以非演示模式暴露在公网。

还原会让当天所有访客的登录失效（设备记录也在快照之外），这是预期行为。
订阅、播放记录、「正在播放」这些演示数据不在快照里，是每次以演示模式启动时按当天
生成的（见设计文档 §6），所以快照放多久都不会过期。

## 上线后的验收清单

- [ ] `https://<域名>` 的登录卡片里列出 4 个演示账号，点一下能填入；
- [ ] 登录后落在媒体库；侧栏 / 底栏没有「新会话」入口，「发现」能正常逛；
- [ ] 在发现页点「订阅」能打开订阅面板，点确认后提示「演示站不会真的订阅和下载……」；
- [ ] 超管登录后：改密码 / 新建成员 / 删除成员 / 删片 / 新建媒体库 / 改任意设置都提示
      「演示站……」且没有生效；
- [ ] 「设置 → 我的设备」里别的访客显示为「其他访客的……」，没有 IP；
- [ ] 电影、动画短片能在网页里直接播放（不弹「是否开启软件转码」）；Sintel、
      Tears of Steel 能切中文字幕；
- [ ] 图片库按月分组，点开大图正常；
- [ ] 「我的订阅」有订阅、「刚刚入库」有卡片；活动页有正在播放、最近播放与观看统计；
- [ ] 小朋友账号只看得到「动画短片」「图片」；朋友账号只看得到「电影」；
- [ ] iOS App 填 `https://<域名>` 能登录、能播放、能逛「发现」，订阅确认时显示同样的提示；
- [ ] `https://<域名>/docs`、`/api/v1/openapi.json` 返回 404；
- [ ] 登录后在同一个浏览器打开 `https://<域名>/api/v1/spec`，返回 403（演示站不开放
      完整接口清单）。

## 改内容 / 改账号

1. 在仓库里改 `demo/content.json` / `demo/accounts.json`（新增影片记得同时更新
   `CREDITS.md`：`python3 demo/fetch_content.py --credits demo/CREDITS.md`），提交后
   再同步到服务器；
2. 重跑第 3 步（只会下载新增的内容）；
3. 删掉 `data/` 与 `golden-data.tar.gz`，从第 4 步重新建站并打快照——**第 4 步的
   `docker compose down` 不能省**：此时 Caddy 还在跑，删掉 `data/` 后的实例是全新未
   初始化的，经 Caddy 暴露在公网就会被人抢注超管：

   ```bash
   docker compose down
   rm -rf data golden-data.tar.gz
   MOVIECLAW_DEMO_MODE=false docker compose up -d movieclaw
   python3 provision.py --server http://127.0.0.1:3000 --media-root /media
   ./reset.sh snapshot
   ```

只改登录页的说明文字或账号描述时，改完 `accounts.json` 执行 `./reset.sh restore` 即可
（以演示模式重启并自检，顺带还原一次），不用重新建站。
