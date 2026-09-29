import AetherCore
import SwiftUI

/// 刷片页的状态：翻页、当前这条的播放器、预取窗口、事件上报（docs/design/reels.md §4、§6）。
///
/// **只有当前这条有播放器**：滑动停稳后销毁上一条、为新的一条建引擎并从片段起点起播。
/// 同时存在两个引擎（4K 杜比视界每个约 135MB 内存）的「预起下一条」留到测出来确实不够快再做。
///
/// **预取窗口**：当前这条出第一个画面后（不和它抢起播的线路），后台把接下来几条要读的字节
/// 按服务端给的范围写进引擎的片源字节缓存：下一条全量（文件头 + 索引 + 起点后约 4 秒），
/// 再往后两条只取文件头与索引（都很小）。计费网络只预取下一条。滑走的条目的预取任务直接取消。
///
/// **事件**：曝光、出画面（带等待时长）、滑走（带看了多久）、看完、接着看、看正片、放不出，
/// 攒满 10 条或离开页面时批量上报；上报失败直接丢弃（只是统计，不重试）。
@Observable
@MainActor
final class ReelsStore {
    private(set) var items: [API.ReelItemView] = []
    /// 滚动位置绑定的「当前条」；滑动停稳后由 `settle()` 切换播放器
    var currentID: String?
    private(set) var loading = false
    private(set) var errorMessage: String?
    private(set) var exhausted = false
    private(set) var player: ReelPlayer?
    private(set) var playerState: ReelPlayer.State = .loading
    private(set) var firstFrameShown = false

    @ObservationIgnored private let api: APIClient
    @ObservationIgnored private let metered: Bool
    @ObservationIgnored private var seed: Int?
    @ObservationIgnored private var nextOffset = 0
    @ObservationIgnored private var prefetchTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var pendingEvents: [API.ReelEventIn] = []
    @ObservationIgnored private var shownAt: ContinuousClock.Instant?
    /// 当前这条已经「接着看 / 看正片」转去播放器页了
    @ObservationIgnored private var handedOff = false

    static let pageSize = 10
    /// 剩几条时拉下一页
    static let loadAheadThreshold = 3

    init(api: APIClient, metered: Bool) {
        self.api = api
        self.metered = metered
    }

    // MARK: - 翻页

    func start() async {
        if items.isEmpty { await loadMore() }
        if currentID == nil { currentID = items.first?.id }
        settle()
    }

    func loadMore() async {
        guard !loading, !exhausted else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await api.reelsFeed(seed: seed, offset: nextOffset, limit: Self.pageSize, modes: "seek")
            seed = page.seed
            nextOffset = page.nextOffset
            exhausted = !page.hasMore
            let known = Set(items.map(\.id))
            items += page.items.filter { $0.play.mode == "seek" && !known.contains($0.id) }
            errorMessage = nil
        } catch {
            errorMessage = "片段加载失败：\(error.localizedDescription)"
        }
    }

    func retry() async {
        errorMessage = nil
        await start()
    }

    // MARK: - 当前条

    /// 滑动停稳：当前条变了就换播放器
    func settle() {
        guard let id = currentID, player?.item.id != id,
              let index = items.firstIndex(where: { $0.id == id }) else { return }
        leaveCurrent()
        let item = items[index]
        firstFrameShown = false
        playerState = .loading
        shownAt = .now
        record(item, kind: "impression", positionMs: item.segment.startMs)
        do {
            let player = try ReelPlayer(item: item)
            player.onStateChange = { [weak self] state in self?.stateChanged(state, of: item) }
            player.onFirstFrame = { [weak self] in self?.firstFrame(of: item, index: index) }
            self.player = player
            player.start(server: api.server)
        } catch {
            playerState = .failed("播放器创建失败")
            record(item, kind: "fail", detail: ["reason": .string("engine_init")])
            schedulePrefetch(after: index)
        }
        if items.count - index <= Self.loadAheadThreshold {
            Task { await loadMore() }
        }
    }

    func togglePause() { player?.togglePause() }

    func pause() { player?.pause() }

    /// 「接着看」：从当前位置转到播放器页（同一个文件，刷片下过的字节直接复用）
    func continueRequest() -> PlayRequest? {
        guard let player else { return nil }
        let item = player.item
        let position = player.position
        record(item, kind: "continue", positionMs: Int(position * 1000), watchedMs: watchedMs(player))
        handedOff = true
        return PlayRequest(mediaItemId: item.title.mediaItemId, season: item.title.episode?.season,
                           episode: item.title.episode?.episode, startSeconds: position,
                           fileId: item.segment.fileId)
    }

    /// 「看正片」：按正常播放的规则起播（续播点或片头、默认版本）
    func openRequest(for item: API.ReelItemView) -> PlayRequest {
        record(item, kind: "open", watchedMs: player.map { watchedMs($0) })
        handedOff = true
        return PlayRequest(mediaItemId: item.title.mediaItemId, season: item.title.episode?.season,
                           episode: item.title.episode?.episode)
    }

    /// 离开刷片页：收掉播放器与预取，把攒着的事件报上去
    func stop() {
        leaveCurrent()
        for task in prefetchTasks.values { task.cancel() }
        prefetchTasks.removeAll()
        Task { await flush() }
    }

    private func leaveCurrent() {
        guard let player else { return }
        // 已经接着看 / 看正片了：这一条是转去正片，不是滑走
        if player.state != .ended, !handedOff {
            record(player.item, kind: "leave", positionMs: Int(player.position * 1000), watchedMs: watchedMs(player))
        }
        player.destroy()
        self.player = nil
        handedOff = false
    }

    private func watchedMs(_ player: ReelPlayer) -> Int {
        Int(max(0, player.position - player.startSeconds) * 1000)
    }

    private func stateChanged(_ state: ReelPlayer.State, of item: API.ReelItemView) {
        guard player?.item.id == item.id else { return }
        playerState = state
        switch state {
        case .ended:
            if let player { record(item, kind: "complete", positionMs: item.segment.endMs, watchedMs: watchedMs(player)) }
        case let .failed(message):
            record(item, kind: "fail", detail: ["reason": .string(message)])
            if let index = items.firstIndex(where: { $0.id == item.id }) { schedulePrefetch(after: index) }
        default:
            break
        }
    }

    private func firstFrame(of item: API.ReelItemView, index: Int) {
        guard player?.item.id == item.id else { return }
        firstFrameShown = true
        let waited = shownAt.map { ContinuousClock.now - $0 } ?? .zero
        let waitMs = Int(waited.components.seconds * 1000 + waited.components.attoseconds / 1_000_000_000_000_000)
        record(item, kind: "first_frame", positionMs: item.segment.startMs, waitMs: waitMs)
        schedulePrefetch(after: index)
    }

    // MARK: - 预取

    private func schedulePrefetch(after index: Int) {
        let window = metered ? 1 : 3
        let targets = Array(items.dropFirst(index + 1).prefix(window))
        let keep = Set(targets.map(\.id))
        for (taskKey, task) in prefetchTasks where !keep.contains(String(taskKey.split(separator: "#")[0])) {
            task.cancel()
            prefetchTasks[taskKey] = nil
        }
        for (position, item) in targets.enumerated() {
            // 下一条全量；再往后只取文件头与索引（起点后几秒那段动辄几十 MB，滑不到就白下了）。
            // 任务按「条目 + 全量/轻量」分开记：一条从后排挪到下一条时，要补上起点那一段——
            // 已经下过的文件头与索引由引擎按缓存覆盖跳过，不会重下
            let full = position == 0
            let taskKey = "\(item.id)#\(full ? "full" : "light")"
            guard prefetchTasks[taskKey] == nil, prefetchTasks["\(item.id)#full"] == nil,
                  let raw = item.play.streamUrl, let url = api.server.resolve(raw),
                  let key = ReelPlayer.cacheKey(for: item) else { continue }
            let ranges = item.play.prefetch
                .filter { full || $0.purpose != "start" }
                .map { (offset: Int64($0.offset), length: Int64($0.length)) }
            prefetchTasks[taskKey] = Task.detached(priority: .utility) {
                _ = await AetherPlayback.prefetchSource(url: url, cacheKey: key, ranges: ranges,
                                                        headers: ["User-Agent": APIClient.userAgent])
            }
        }
    }

    // MARK: - 事件

    private func record(_ item: API.ReelItemView, kind: String, positionMs: Int? = nil, watchedMs: Int? = nil,
                        waitMs: Int? = nil, detail: [String: API.JSONValue]? = nil) {
        var detail = detail ?? [:]
        detail["network"] = .string(NetworkCost.shared.interface)
        pendingEvents.append(API.ReelEventIn(
            reelId: item.id, kind: kind, mode: item.play.mode, mediaItemId: item.title.mediaItemId,
            fileId: item.segment.fileId, positionMs: positionMs, watchedMs: watchedMs, waitMs: waitMs,
            detail: detail
        ))
        if pendingEvents.count >= 10 { Task { await flush() } }
    }

    private func flush() async {
        let batch = pendingEvents
        pendingEvents.removeAll()
        guard !batch.isEmpty else { return }
        _ = try? await api.reelsEvents(body: API.ReelEventBatch(events: batch))
    }
}
