import SwiftUI
import UIKit

/// 头像页签上的账号手势（仿 Instagram，2026-09-29 用户拍板）：**长按**弹出切换账号抽屉，**双击**切回上一个账号。
/// 家里几个人共用 App、切换账号是高频操作：不用先进「我的」页、不用再点一层，在哪个页签上都能直接切。
///
/// 系统标签栏（iOS 26 液态玻璃的 `UITabBar`）没有给单个页签挂手势的接口，SwiftUI 的 `Tab` 也没有；
/// 用户定过不自己绘制标签栏。这里从视图所在窗口找到标签栏，在**整条标签栏**上挂长按与双击两个识别器，
/// 按手指落点命中的页签按钮判断是不是头像页签——页签按钮的读屏名就是 SwiftUI 给它设的
/// `accessibilityLabel`（「我的」），只用公开属性，不碰私有视图。两个识别器都不吞触摸、与系统手势并存：
/// - 单击照常切页签，在当前页签上再点照常回到顶层；
/// - 按住时系统照常把选中光圈移到头像上并放大（按压反馈），0.45 秒后弹抽屉；松手后系统会顺带选中
///   「我的」页签，落在抽屉后面，不影响；
/// - 按住再拖是系统的「滑过页签切换」：手指一动（超过 10pt）长按就不成立，两者互不干扰。
///
/// 2026-09-29 在 iOS 26.5 模拟器上实测：长按命中「我的」；约 0.1 秒内点两下触发双击；间隔 1 秒点两下
/// 不触发（只算两次普通点选）；按住期间系统光圈与抽屉不打架。
struct TabBarAccountGestures: UIViewRepresentable {
    /// 头像页签的读屏名（MainTab.more.title）
    let avatarLabel: String
    let onLongPress: () -> Void
    let onDoubleTap: () -> Void
    /// 头像页签按钮在窗口里的位置（首次提示气泡对准它）
    let onAvatarFrame: (CGRect) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UIView {
        let view = UIView(frame: .zero)
        view.isUserInteractionEnabled = false
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        let coordinator = context.coordinator
        coordinator.parent = self
        // 等视图进窗口、标签栏建好之后再挂（首次更新时窗口可能还是 nil）
        DispatchQueue.main.async { coordinator.attach(from: view) }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var parent: TabBarAccountGestures?
        private weak var tabBar: UITabBar?

        func attach(from view: UIView) {
            guard let root = view.window?.rootViewController, let bar = Self.findTabBar(from: root) else { return }
            if bar !== tabBar {
                tabBar = bar
                let longPress = UILongPressGestureRecognizer(target: self, action: #selector(longPressed(_:)))
                longPress.minimumPressDuration = 0.45
                longPress.cancelsTouchesInView = false
                longPress.delegate = self
                bar.addGestureRecognizer(longPress)
                let doubleTap = UITapGestureRecognizer(target: self, action: #selector(doubleTapped(_:)))
                doubleTap.numberOfTapsRequired = 2
                doubleTap.cancelsTouchesInView = false
                doubleTap.delaysTouchesEnded = false
                doubleTap.delegate = self
                bar.addGestureRecognizer(doubleTap)
            }
            if let button = avatarButton(in: bar) {
                parent?.onAvatarFrame(button.convert(button.bounds, to: nil))
            }
        }

        @objc private func longPressed(_ recognizer: UILongPressGestureRecognizer) {
            guard recognizer.state == .began, hitsAvatar(recognizer) else { return }
            parent?.onLongPress()
        }

        @objc private func doubleTapped(_ recognizer: UITapGestureRecognizer) {
            guard recognizer.state == .ended, hitsAvatar(recognizer) else { return }
            parent?.onDoubleTap()
        }

        /// 手指落点命中的是不是头像页签：沿命中视图往上找，读屏名对得上即是
        private func hitsAvatar(_ recognizer: UIGestureRecognizer) -> Bool {
            guard let bar = tabBar, let label = parent?.avatarLabel else { return false }
            var view = bar.hitTest(recognizer.location(in: bar), with: nil)
            while let current = view, current !== bar {
                if current.accessibilityLabel == label { return true }
                view = current.superview
            }
            return false
        }

        /// 头像页签按钮：读屏名对得上的那个；读屏名还没设好时退到最右边的页签按钮（头像页签永远在最右）
        private func avatarButton(in bar: UITabBar) -> UIView? {
            var buttons: [UIView] = []
            func collect(_ view: UIView) {
                if view.isAccessibilityElement, !view.isHidden, view.bounds.width > 0 { buttons.append(view) }
                view.subviews.forEach(collect)
            }
            collect(bar)
            let label = parent?.avatarLabel
            return buttons.first { $0.accessibilityLabel == label }
                ?? buttons.max { $0.convert($0.bounds, to: bar).maxX < $1.convert($1.bounds, to: bar).maxX }
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            true
        }

        private static func findTabBar(from controller: UIViewController) -> UITabBar? {
            if let tabs = controller as? UITabBarController { return tabs.tabBar }
            for child in controller.children {
                if let found = findTabBar(from: child) { return found }
            }
            return nil
        }
    }
}

/// 第一次提示账号手势：本机登录了不止一个账号时才有用，所以只在账号数超过一个、且没提示过时出现一次
/// （刚添加第二个账号时立刻出现；更新前就有多个账号的，更新后第一次打开也出现一次）。
/// 气泡悬在头像页签正上方、右边与头像对齐（不画小尖角：实心尖角压在液态玻璃上对不上质感，
/// 挨着头像本身就说明了指的是它），点一下或 6 秒后收起。
struct AccountGestureTip: View {
    /// 头像页签在窗口里的位置
    let avatarFrame: CGRect
    let dismiss: () -> Void

    var body: some View {
        GeometryReader { proxy in
            let origin = proxy.frame(in: .global).origin
            // 气泡右边对齐头像右边、底边在头像上方 10pt；两边至少留 12pt
            let trailing = min(proxy.size.width - 12, avatarFrame.maxX - origin.x)
            let bottom = max(0, avatarFrame.minY - origin.y - 10)
            VStack(alignment: .leading, spacing: 6) {
                Label("长按头像：切换账号", systemImage: "hand.tap")
                Label("双击头像：切回上一个账号", systemImage: "arrow.left.arrow.right")
            }
            .font(.subheadline)
            .foregroundStyle(Theme.text)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .glassEffect(.regular, in: .rect(cornerRadius: 18))
            .fixedSize()
            .onTapGesture(perform: dismiss)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityHint("轻点关闭")
            .accessibilityIdentifier("account-gesture-tip")
            .frame(width: trailing, height: bottom, alignment: .bottomTrailing)
        }
        .ignoresSafeArea()
        .transition(.opacity)
    }
}
