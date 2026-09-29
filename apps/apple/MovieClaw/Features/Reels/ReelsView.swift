import AVFoundation
import SwiftUI

/// 刷片：全屏沉浸、上下整页滑动，每页从一部电影 / 一部剧里挑出的 30～60 秒（docs/design/reels.md）。
///
/// 版式（竖屏）：背景是封面放大模糊；影片居中成一条 16:9 的横带，没出第一帧前先显示封面（服务端抓的
/// 就是起点那一帧，出画面时无缝接上）；横带下面是片名与信息，底部两个按钮：
/// - **接着看**：从当前位置转到播放器页（同一个文件，刷片下过的字节直接复用）；
/// - **看正片**：按正常播放的规则起播（续播点或片头）。
/// 点画面暂停 / 继续；放到片段终点停下，给「重播」与「接着看」。
///
/// 播放器页是根部的全屏弹层，与本页不能同时在：两个按钮都交给 `onPlay`，由媒体库首页先收起本页、
/// 收起之后再起播。
struct ReelsView: View {
    let onPlay: (PlayRequest) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var store: ReelsStore

    init(api: APIClient, metered: Bool, onPlay: @escaping (PlayRequest) -> Void) {
        self.onPlay = onPlay
        _store = State(initialValue: ReelsStore(api: api, metered: metered))
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.black.ignoresSafeArea()
            if store.items.isEmpty {
                emptyState
            } else {
                pager
            }
            closeButton
        }
        .preferredColorScheme(.dark)
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .task { await store.start() }
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            let audio = AVAudioSession.sharedInstance()
            try? audio.setCategory(.playback, mode: .moviePlayback, policy: .longFormVideo)
            try? audio.setActive(true)
        }
        .onDisappear {
            store.stop()
            UIApplication.shared.isIdleTimerDisabled = false
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { store.pause() }
        }
    }

    private var pager: some View {
        ScrollView(.vertical) {
            LazyVStack(spacing: 0) {
                ForEach(store.items, id: \.id) { item in
                    ReelPage(item: item, store: store, isCurrent: item.id == store.currentID,
                             onContinue: continueWatching, onOpen: { open(item) })
                        .containerRelativeFrame([.horizontal, .vertical])
                }
                if store.exhausted {
                    Text("刷完了")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 40)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.paging)
        .scrollPosition(id: $store.currentID)
        .scrollIndicators(.hidden)
        .onScrollPhaseChange { _, phase in
            // 滑动停稳才换播放器：拖动途中 currentID 会跟着变，不能每变一次就起一个引擎
            if phase == .idle { store.settle() }
        }
        .ignoresSafeArea()
    }

    @ViewBuilder
    private var emptyState: some View {
        VStack(spacing: 14) {
            if store.loading {
                ProgressView()
                Text("正在挑片段…")
            } else if let message = store.errorMessage {
                Text(message)
                Button("重试") { Task { await store.retry() } }
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

    private var closeButton: some View {
        Button { dismiss() } label: {
            Image(systemName: "xmark")
                .font(.body.weight(.semibold))
                .frame(width: 36, height: 36)
        }
        .buttonStyle(.glass)
        .buttonBorderShape(.circle)
        .accessibilityLabel("关闭")
        .accessibilityIdentifier("reels-close")
        .padding(.leading, Theme.pagePadding)
        .padding(.top, 8)
    }

    private func continueWatching() {
        guard let request = store.continueRequest() else { return }
        onPlay(request)
    }

    private func open(_ item: API.ReelItemView) {
        onPlay(store.openRequest(for: item))
    }
}

/// 刷片的一页
private struct ReelPage: View {
    let item: API.ReelItemView
    let store: ReelsStore
    let isCurrent: Bool
    let onContinue: () -> Void
    let onOpen: () -> Void

    @Environment(\.api) private var api

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            ZStack {
                backdrop(size: geo.size)
                VStack(spacing: 0) {
                    Spacer(minLength: geo.safeAreaInsets.top + 56)
                    videoBox(width: width, height: width * 9 / 16)
                    info
                        .padding(.horizontal, Theme.pagePadding + 4)
                        .padding(.top, 18)
                    Spacer(minLength: 16)
                    actions
                        .padding(.horizontal, Theme.pagePadding + 4)
                        .padding(.bottom, geo.safeAreaInsets.bottom + 28)
                }
            }
        }
        .ignoresSafeArea()
    }

    /// 封面放大模糊铺底，再压一层渐暗：横带之外的上下两块不至于一片死黑
    private func backdrop(size: CGSize) -> some View {
        ZStack {
            RemoteImage(url: api.image(item.coverUrl, .landscapeCard), contentMode: .fill)
                .frame(width: size.width, height: size.height)
                .blur(radius: 48)
                .opacity(0.5)
                .clipped()
            LinearGradient(colors: [.black.opacity(0.55), .black.opacity(0.2), .black.opacity(0.75)],
                           startPoint: .top, endPoint: .bottom)
        }
        .accessibilityHidden(true)
    }

    private var player: ReelPlayer? {
        guard isCurrent, let player = store.player, player.item.id == item.id else { return nil }
        return player
    }

    private func videoBox(width: CGFloat, height: CGFloat) -> some View {
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
        .overlay(alignment: .bottom) { progressBar }
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
                Image(systemName: "play.fill")
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.9))
                    .shadow(radius: 8)
            case .ended:
                endedOverlay
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

    private var endedOverlay: some View {
        ZStack {
            Color.black.opacity(0.55)
            HStack(spacing: 14) {
                Button { store.player?.replay() } label: {
                    Label("重播", systemImage: "arrow.counterclockwise")
                }
                .buttonStyle(.glass)
                Button(action: onContinue) {
                    Label("接着看", systemImage: "play.fill")
                        .foregroundStyle(.black)
                }
                .buttonStyle(.glassProminent)
            }
            .font(.subheadline.weight(.semibold))
        }
    }

    /// 片段进度：横带底边一条细线
    private var progressBar: some View {
        TimelineView(.periodic(from: .now, by: 0.25)) { _ in
            GeometryReader { geo in
                Rectangle()
                    .fill(.white.opacity(0.85))
                    .frame(width: geo.size.width * (player?.progress ?? 0), height: 2)
                    .frame(maxHeight: .infinity, alignment: .bottom)
            }
        }
        .frame(height: 2)
        .opacity(player == nil ? 0 : 1)
        .allowsHitTesting(false)
    }

    private var info: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.title.name)
                .font(.title3.weight(.bold))
                .foregroundStyle(Theme.text)
                .lineLimit(2)
            if !metaLine.isEmpty {
                Text(metaLine)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
            }
            if let episode = item.title.episode {
                Text(episodeLine(episode))
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
            }
            if let tagline = item.title.tagline, !tagline.isEmpty {
                Text(tagline)
                    .font(.footnote)
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var metaLine: String {
        var parts: [String] = []
        if let year = item.title.year { parts.append(String(year)) }
        if let rating = item.title.rating, rating > 0 { parts.append(String(format: "%.1f 分", rating)) }
        parts += item.title.genres.prefix(2)
        return parts.joined(separator: " · ")
    }

    private func episodeLine(_ episode: API.ReelEpisodeView) -> String {
        let code = "第 \(episode.season) 季第 \(episode.episode) 集"
        guard let name = episode.name, !name.isEmpty else { return code }
        return "\(code) · \(name)"
    }

    private var actions: some View {
        HStack(spacing: 12) {
            Button(action: onContinue) {
                Label("接着看", systemImage: "play.fill")
                    .foregroundStyle(.black)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.glassProminent)
            .disabled(!isCurrent || store.player == nil)
            .accessibilityIdentifier("reels-continue")
            Button(action: onOpen) {
                Label("看正片", systemImage: "film")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.glass)
            .accessibilityIdentifier("reels-open")
        }
        .font(.subheadline.weight(.semibold))
        .controlSize(.large)
    }
}
