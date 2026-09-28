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
| `reset.sh` | 黄金快照（`snapshot`）与每日还原（`restore`） |
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

## 部署

服务器上的演示站目录（例如 `/srv/movieclaw-demo`）就是本目录的一份拷贝，
`data/`、`media/` 会建在它旁边。以下命令都在这个目录里执行，需要 root。
服务器建议 2 核 / 4 GB / 40 GB 磁盘起步，带宽越大越好（影片码率 2～6 Mbps）。

### 1. 构建镜像（在开发机上）

演示站必须用本分支（`feat/demo`）构建的镜像：官方镜像没有演示模式，也没有
iOS App 需要的设备登录接口。

```bash
# 服务器是 x86_64 就加 PLATFORM；Apple Silicon 上交叉构建会慢一些
TAG=demo-$(git rev-parse --short HEAD) PLATFORM=linux/amd64 ./scripts/build-image.sh
docker save movieclaw:demo-<sha> | gzip | ssh root@<VPS> 'gunzip | docker load'
```

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

### 4. 建站（演示模式关闭）

```bash
MOVIECLAW_DEMO_MODE=false docker compose up -d movieclaw
python3 provision.py --server http://127.0.0.1:3000 --media-root /media
# 服务器上没有 python3 时，借镜像里的：
# docker run --rm --network host --entrypoint python -v "$PWD:/work" -w /work \
#     "$MOVIECLAW_DEMO_IMAGE" provision.py --server http://127.0.0.1:3000 --media-root /media
```

脚本会等扫描、TMDB 刮削、缩略图与进度条预览都生成完（十来分钟），期间可以重跑，
已完成的步骤会跳过。最后应当看到每个库「已识别 N / 期望 N」和 4 个账号都能登录。

> 注意：这一步服务以普通模式运行、只监听 127.0.0.1，不要在这时开放公网访问。

### 5. 打黄金快照，打开演示模式

```bash
./reset.sh snapshot          # 停服务 → data/ 打包成 golden-data.tar.gz → 启动
docker compose up -d         # 默认 MOVIECLAW_DEMO_MODE=true，同时拉起 Caddy
```

### 6. 每天还原

```bash
crontab -e
# 每天 4:17 还原到黄金快照
17 4 * * * cd /srv/movieclaw-demo && ./reset.sh restore >> reset.log 2>&1
```

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
- [ ] `https://<域名>/docs`、`/api/v1/openapi.json` 返回 404。

## 改内容 / 改账号

1. 在仓库里改 `demo/content.json` / `demo/accounts.json`（新增影片记得同时更新
   `CREDITS.md`：`python3 demo/fetch_content.py --credits demo/CREDITS.md`），提交后
   再同步到服务器；
2. 重跑第 3 步（只会下载新增的内容）；
3. 删掉 `data/` 与 `golden-data.tar.gz`，从第 4 步重新建站并打快照。

只改登录页的说明文字或账号描述时，改完 `accounts.json` 重启容器即可，不用重新建站。
