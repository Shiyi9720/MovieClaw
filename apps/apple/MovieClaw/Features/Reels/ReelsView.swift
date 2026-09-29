import AVFoundation
import SwiftUI

/// 片段：上下整页滑动，每页从一部电影 / 一部剧里挑出的 30～60 秒（docs/design/reels.md）。
///
/// 版式对齐 Instagram Reels / 抖音（2026-09-30 用户给的参照图）：
/// - 媒体库导航栈里压栈打开，**底部标签栏保留**（停在这页时标签栏不随滑动收起）；不带返回键，
///   再点一次「媒体库」页签回到媒体库首页；左上是 iOS 大标题「片段」，右上一个玻璃胶囊「全部 ⌄」，
///   点开选「全部」或某个类型；
/// - 纯黑底，影片居中成一条 16:9 的横带；没出第一帧前先显示封面（服务端抓的就是起点那一帧）；
/// - 右下角一列**无底色**的白色图标按钮（不用毛玻璃，和画面融在一起，带投影保证亮画面上也看得清）：
///   收藏、播放、已看、分享；
/// - 左下角左对齐：最上面一行是导演（剧集是主创）——对应 TikTok / Instagram 里作者头像与名字的位置，
///   点了进人物页；然后是片名 + 年份、评分 · 类型、剧集的季集与集名、两行简介；
/// - 最底下一条细进度线（片段内进度），右边是「在片中的位置 / 片长」。
///
/// 播放按钮：点一下从当前位置转到播放器页（同一个文件，刷片下过的字节直接复用），长按可选「从头看」。
/// 点画面暂停 / 继续，放到片段终点停下、再点重播。
struct ReelsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(\.scenePhase) private var scenePhase
    @State private var store: ReelsStore?
    @State private var visible = false
    @State private var sharing: API.ReelItemView?

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black
                if let store {
                    if store.items.isEmpty {
                        emptyState(store)
                    } else {
                        pager(store, size: geo.size, insets: geo.safeAreaInsets)
                    }
                }
            }
            .ignoresSafeArea()
        }
        .background(Color.black.ignoresSafeArea())
        // 标题不用系统的大标题：系统大标题跟着滚动视图走，一滑到下一条就缩成正中的小字。
        // 这里放在左上角的工具栏位、去掉玻璃底，字号与媒体库首页的标题一致，滑动时不变
        .navigationTitle("")
        .toolbarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Text("片段")
                    .font(.title.weight(.bold))
                    .foregroundStyle(.white)
                    .fixedSize()
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("reels-title")
            }
            .sharedBackgroundVisibility(.hidden)
            ToolbarItem(placement: .topBarTrailing) { genreMenu }
        }
        .preferredColorScheme(.dark)
        .task {
            if store == nil { store = ReelsStore(api: api, metered: NetworkCost.shared.isMetered) }
            await store?.start()
        }
        .onAppear {
            visible = true
            UIApplication.shared.isIdleTimerDisabled = true
            let audio = AVAudioSession.sharedInstance()
            try? audio.setCategory(.playback, mode: .moviePlayback, policy: .longFormVideo)
            try? audio.setActive(true)
            store?.resume()
        }
        .onDisappear {
            visible = false
            store?.suspend()
            UIApplication.shared.isIdleTimerDisabled = false
        }
        // 播放器页是根部的全屏弹层：盖上来时收掉刷片的引擎，收起后当前这条重新起播
        .onChange(of: router.player == nil) { _, closed in
            if closed {
                if visible { store?.resume() }
            } else {
                store?.suspend()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { store?.pause() }
        }
        .sheet(isPresented: Binding(get: { sharing != nil }, set: { if !$0 { sharing = nil } })) {
            if let item = sharing {
                LibraryShareSheet(
                    target: .item(libraryId: item.title.libraryId, mediaItemId: item.title.mediaItemId),
                    title: item.title.name, kind: item.title.kind, year: item.title.year,
                    posterUrl: item.title.posterUrl
                )
                .sheetFeedback()
            }
        }
    }

    // MARK: - 顶部类型选择

    private var genreMenu: some View {
        Menu {
            Picker("类型", selection: Binding(get: { store?.genre }, set: { store?.selectGenre($0) })) {
                Text("全部").tag(String?.none)
                ForEach(store?.genres ?? [], id: \.name) { genre in
                    Text(genre.name).tag(String?.some(genre.name))
                }
            }
        } label: {
            // 右上角的工具栏位由系统画液态玻璃底，这里只给内容
            HStack(spacing: 5) {
                Text(store?.genre ?? "全部")
                    .font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
        }
        .accessibilityLabel("类型：\(store?.genre ?? "全部")")
        .accessibilityIdentifier("reels-genre")
    }

    // MARK: - 翻页

    private func pager(_ store: ReelsStore, size: CGSize, insets: EdgeInsets) -> some View {
        ScrollView(.vertical) {
            LazyVStack(spacing: 0) {
                ForEach(store.items, id: \.id) { item in
                    ReelPage(
                        item: item, store: store, isCurrent: item.id == store.currentID, insets: insets,
                        canCreateShareLink: permissions.isAdmin,
                        onPlay: { play(from: store, restart: false, item: item) },
                        onPlayFromStart: { play(from: store, restart: true, item: item) },
                        onShare: {
                            store.pause()
                            sharing = item
                        },
                        onOpenPerson: { router.push(.person(tmdbId: $0)) }
                    )
                    .frame(width: size.width + insets.leading + insets.trailing,
                           height: size.height + insets.top + insets.bottom)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.paging)
        .scrollPosition(id: Binding(get: { store.currentID }, set: { store.currentID = $0 }))
        .scrollIndicators(.hidden)
        .onScrollPhaseChange { _, phase in
            // 滑动停稳才换播放器：拖动途中 currentID 会跟着变，不能每变一次就起一个引擎
            if phase == .idle { store.settle() }
        }
    }

    private func play(from store: ReelsStore, restart: Bool, item: API.ReelItemView) {
        let request = restart ? store.openRequest(for: item) : (store.continueRequest() ?? store.openRequest(for: item))
        store.suspend()
        router.play(request)
    }

    // MARK: - 空态

    private func emptyState(_ store: ReelsStore) -> some View {
        VStack(spacing: 14) {
            if store.loading {
                ProgressView()
                Text("正在挑片段…")
            } else if let message = store.errorMessage {
                Text(message)
                Button("重试") { Task { await store.retry() } }
                    .buttonStyle(.glass)
            } else if let genre = store.genre {
                Text("「\(genre)」里还没有能刷的片子")
                Button("看全部") { store.selectGenre(nil) }
                    .buttonStyle(.glass)
            } else {
                Text("片库里还没有能刷的片子")
                Text("目前支持 MKV / MP4 的电影与剧集")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
            }
        }
        .font(.subheadline)
        .foregroundStyle(Theme.textMuted)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// 片段的一页
private struct ReelPage: View {
    let item: API.ReelItemView
    let store: ReelsStore
    let isCurrent: Bool
    let insets: EdgeInsets
    let canCreateShareLink: Bool
    let onPlay: () -> Void
    let onPlayFromStart: () -> Void
    let onShare: () -> Void
    let onOpenPerson: (Int) -> Void

    @Environment(\.api) private var api

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            ZStack(alignment: .bottom) {
                Color.black
                // 横带的中心放在屏幕 46% 高处（参照图：略高于正中，给底部信息留地方）
                videoBand(width: width, height: width * 9 / 16)
                    .position(x: width / 2, y: geo.size.height * 0.46)
                VStack(spacing: 12) {
                    HStack(alignment: .bottom, spacing: 12) {
                        info
                        actions
                    }
                    progressLine
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.bottom, insets.bottom + 10)
            }
        }
    }

    private var player: ReelPlayer? {
        guard isCurrent, let player = store.player, player.item.id == item.id else { return nil }
        return player
    }

    // MARK: 画面

    private func videoBand(width: CGFloat, height: CGFloat) -> some View {
        ZStack {
            Color.black
            RemoteImage(url: api.image(item.coverUrl, .landscapeCard), contentMode: .fit)
            if let player {
                EngineSurface(engineView: player.core.view)
                    .id(ObjectIdentifier(player))
                    .opacity(store.firstFrameShown ? 1 : 0)
            }
            overlay
        }
        .frame(width: width, height: height)
        .clipped()
        .contentShape(Rectangle())
        .onTapGesture { if isCurrent { store.togglePause() } }
        .accessibilityIdentifier("reels-video")
    }

    @ViewBuilder
    private var overlay: some View {
        if isCurrent {
            switch store.playerState {
            case .loading where !store.firstFrameShown:
                ProgressView().tint(.white)
            case .paused:
                centerGlyph("play.fill")
            case .ended:
                centerGlyph("arrow.counterclockwise")
            case let .failed(message):
                VStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle")
                    Text("这一段放不出来，往下滑换一条")
                        .font(.footnote)
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(2)
                }
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 24)
            default:
                EmptyView()
            }
        }
    }

    private func centerGlyph(_ symbol: String) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 36, weight: .semibold))
            .foregroundStyle(.white.opacity(0.92))
            .shadow(color: .black.opacity(0.5), radius: 8)
    }

    // MARK: 右下角按钮

    private var actions: some View {
        let favorite = store.isFavorite(item)
        let played = store.isPlayed(item)
        return VStack(spacing: 18) {
            ReelActionButton(symbol: favorite ? "heart.fill" : "heart", title: "收藏",
                             tint: favorite ? Color(red: 1, green: 0.27, blue: 0.35) : .white) {
                Task { await store.toggleFavorite(item) }
            }
            .accessibilityValue(favorite ? "已收藏" : "未收藏")
            .accessibilityIdentifier("reels-favorite")
            ReelActionButton(symbol: "play.fill", title: "播放", action: onPlay)
                .contextMenu {
                    Button("从这里接着看", systemImage: "play.fill", action: onPlay)
                    Button("从头看", systemImage: "backward.end.fill", action: onPlayFromStart)
                }
                .accessibilityIdentifier("reels-play")
            ReelActionButton(symbol: played ? "checkmark.circle.fill" : "checkmark.circle", title: "已看",
                             tint: played ? Theme.success : .white) {
                Task { await store.togglePlayed(item) }
            }
            .accessibilityValue(played ? "已看过" : "没看过")
            .accessibilityIdentifier("reels-played")
            if canCreateShareLink {
                ReelActionButton(symbol: "paperplane", title: "分享", action: onShare)
                    .accessibilityIdentifier("reels-share")
            } else {
                ShareLink(item: shareText) {
                    ReelActionLabel(symbol: "paperplane", title: "分享", tint: .white)
                }
                .accessibilityIdentifier("reels-share")
            }
        }
    }

    private var shareText: String {
        var text = "《\(item.title.name)》"
        if let year = item.title.year { text += "（\(year)）" }
        if let episode = item.title.episode { text += " 第 \(episode.season) 季第 \(episode.episode) 集" }
        return text
    }

    // MARK: 左下角信息

    private var info: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !item.title.directors.isEmpty { directorRow }
            titleBlock
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// 导演（剧集是主创）：头像 + 名字 + 描边小标签，对应 Instagram「头像 · 作者名 · 关注」那一行
    private var directorRow: some View {
        let people = item.title.directors
        let lead = people[0]
        return Button {
            if let id = lead.tmdbPersonId { onOpenPerson(id) }
        } label: {
            HStack(spacing: 8) {
                RemoteImage(url: api.image(lead.avatarUrl), placeholderSymbol: "person.fill")
                    .frame(width: 28, height: 28)
                    .clipShape(Circle())
                    .overlay(Circle().stroke(.white.opacity(0.25), lineWidth: 0.5))
                Text(people.map(\.name).joined(separator: " / "))
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Text(item.title.kind == "tv" ? "主创" : "导演")
                    .font(.caption2.weight(.semibold))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .overlay(Capsule().stroke(.white.opacity(0.7), lineWidth: 1))
            }
            .foregroundStyle(.white)
            .shadow(color: .black.opacity(0.55), radius: 3, y: 1)
        }
        .buttonStyle(.plain)
        .disabled(lead.tmdbPersonId == nil)
        .accessibilityLabel("\(item.title.kind == "tv" ? "主创" : "导演")：\(people.map(\.name).joined(separator: "、"))")
        .accessibilityIdentifier("reels-director")
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(item.title.name)
                    .font(.headline.weight(.bold))
                    .lineLimit(2)
                if let year = item.title.year {
                    Text(String(year))
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.72))
                }
            }
            if !facts.isEmpty {
                Text(facts)
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.78))
                    .lineLimit(1)
            }
            if let episode = item.title.episode {
                Text(episodeLine(episode))
                    .font(.footnote.weight(.semibold))
                    .lineLimit(1)
            }
            if let overview, !overview.isEmpty {
                Text(overview)
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.82))
                    .lineLimit(2)
            }
        }
        .foregroundStyle(.white)
        .shadow(color: .black.opacity(0.55), radius: 3, y: 1)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("reels-info")
    }

    /// 剧集优先用分集简介，没有就用整剧的
    private var overview: String? {
        item.title.episode?.overview ?? item.title.overview
    }

    /// 评分 · 类型（片长不再重复写：进度线右边的总时长就是）
    private var facts: String {
        var parts: [String] = []
        if let rating = item.title.rating, rating > 0 { parts.append(String(format: "★ %.1f", rating)) }
        if !item.title.genres.isEmpty { parts.append(item.title.genres.prefix(2).joined(separator: " / ")) }
        return parts.joined(separator: " · ")
    }

    private func episodeLine(_ episode: API.ReelEpisodeView) -> String {
        let code = "第 \(episode.season) 季第 \(episode.episode) 集"
        guard let name = episode.name, !name.isEmpty else { return code }
        return "\(code) · \(name)"
    }

    // MARK: 进度线

    /// 细线是这一段的进度；右边的时间是「在整部片里放到哪 / 整部片多长」（剧集是这一集），
    /// 让人知道这一段出自片子的什么位置
    private var progressLine: some View {
        TimelineView(.periodic(from: .now, by: 0.25)) { _ in
            HStack(spacing: 10) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.white.opacity(0.22))
                        Capsule().fill(.white.opacity(0.9))
                            .frame(width: geo.size.width * (player?.progress ?? 0))
                    }
                }
                .frame(height: 2)
                Text(timeText)
                    .font(.caption2.weight(.medium).monospacedDigit())
                    .foregroundStyle(.white.opacity(0.78))
                    .shadow(color: .black.opacity(0.5), radius: 2, y: 1)
                    .fixedSize()
                    .accessibilityIdentifier("reels-time")
            }
        }
        .frame(height: 14)
        .allowsHitTesting(false)
    }

    private var timeText: String {
        let position = player?.position ?? Double(item.segment.startMs) / 1000
        let total = item.segment.durationMs.map { Double($0) / 1000 } ?? player?.core.duration
        guard let total, total > 0 else { return Self.clock(position, long: position >= 3600) }
        let long = total >= 3600
        return "\(Self.clock(position, long: long)) / \(Self.clock(total, long: long))"
    }

    /// 1:36:17 / 45:08：一小时以上的片子两边都带小时位，对齐不跳
    private static func clock(_ seconds: Double, long: Bool) -> String {
        let total = max(0, Int(seconds.rounded(.down)))
        let h = total / 3600, m = total % 3600 / 60, s = total % 60
        return long ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

/// 右下角的按钮：白色图标 + 小字，无底色（不用毛玻璃，和画面融在一起），带投影保证亮画面上也看得清
private struct ReelActionButton: View {
    let symbol: String
    let title: String
    var tint: Color = .white
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ReelActionLabel(symbol: symbol, title: title, tint: tint)
        }
        .buttonStyle(.plain)
    }
}

private struct ReelActionLabel: View {
    let symbol: String
    let title: String
    let tint: Color

    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: symbol)
                .font(.system(size: 26, weight: .regular))
                .frame(height: 30)
            Text(title)
                .font(.caption2.weight(.medium))
        }
        .foregroundStyle(tint)
        .shadow(color: .black.opacity(0.5), radius: 3, y: 1)
        .frame(width: 52)
        .contentShape(Rectangle())
    }
}
