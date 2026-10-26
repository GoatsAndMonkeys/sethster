import SwiftUI
import UIKit

/// Fetches images with the API's auth header (AsyncImage cannot send headers) and caches them in memory.
/// Failures (e.g. 404 = no artwork/portrait) are remembered so placeholders don't refetch.
actor ImageLoader {
    static let shared = ImageLoader()
    private let cache = NSCache<NSURL, UIImage>()
    private var misses: Set<URL> = []
    private var inflight: [URL: Task<UIImage?, Never>] = [:]

    func image(for url: URL, headers: [String: String]) async -> UIImage? {
        if let img = cache.object(forKey: url as NSURL) { return img }
        if misses.contains(url) { return nil }
        if let t = inflight[url] { return await t.value }
        let task = Task<UIImage?, Never> {
            var req = URLRequest(url: url)
            headers.forEach { req.setValue($1, forHTTPHeaderField: $0) }
            guard let (data, resp) = try? await URLSession.shared.data(for: req),
                  (resp as? HTTPURLResponse)?.statusCode ?? 200 < 300,
                  let img = UIImage(data: data) else { return nil }
            return img.preparingForDisplay() ?? img
        }
        inflight[url] = task
        let img = await task.value
        inflight[url] = nil
        if let img { cache.setObject(img, forKey: url as NSURL) } else { misses.insert(url) }
        return img
    }
}

/// Server image at `path`, or `placeholder` while loading / if missing.
struct RemoteImage<Placeholder: View>: View {
    @Environment(AppModel.self) private var model
    let path: String?
    /// Portraits: square by half crop / half stretch instead of a full crop.
    var balanced: Bool = false
    @ViewBuilder var placeholder: () -> Placeholder
    @State private var image: UIImage?

    var body: some View {
        let url = model.api.resolve(path)
        ZStack {
            if let image {
                // Decorative: never let an oversized fill image take taps outside its clipped frame.
                Image(uiImage: image).resizable().scaledToFill().allowsHitTesting(false)
            } else {
                placeholder()
            }
        }
        .task(id: url) {
            guard let url else { image = nil; return }
            let headers = path?.hasPrefix("http") == true ? [:] : model.api.authHeaders
            let img = await ImageLoader.shared.image(for: url, headers: headers)
            image = balanced ? img?.balancedSquare() : img
        }
    }
}

/// Square artwork for a performance: folder art from the NAS when present, otherwise a typographic cover.
struct ArtworkView: View {
    let path: String?
    let title: String
    let seed: String
    var cornerRadius: CGFloat = 8

    var body: some View {
        Color.clear
            .aspectRatio(1, contentMode: .fit)
            .overlay {
                RemoteImage(path: path) { GeneratedCover(title: title, seed: seed) }
            }
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(.primary.opacity(0.08), lineWidth: 0.5)
            }
    }
}

enum Palette {
    /// Muted, editorial duotones, picked deterministically from a seed (usually the collection).
    /// Pencil-and-paper tones: charcoal, graphite and warm grays, varied deterministically by a seed (usually
    /// the collection). Colour is reserved for the single ember accent, drawn as the cover's rule.
    static let pairs: [(Color, Color)] = [
        (Color(red: 0.11, green: 0.10, blue: 0.10), Color(red: 0.30, green: 0.29, blue: 0.28)), // charcoal
        (Color(red: 0.20, green: 0.19, blue: 0.18), Color(red: 0.45, green: 0.43, blue: 0.40)), // graphite
        (Color(red: 0.24, green: 0.21, blue: 0.18), Color(red: 0.52, green: 0.47, blue: 0.41)), // sepia
        (Color(red: 0.16, green: 0.17, blue: 0.19), Color(red: 0.38, green: 0.40, blue: 0.43)), // slate
        (Color(red: 0.27, green: 0.25, blue: 0.23), Color(red: 0.58, green: 0.55, blue: 0.51)), // pewter
        (Color(red: 0.14, green: 0.13, blue: 0.12), Color(red: 0.36, green: 0.33, blue: 0.30)), // umber-black
    ]

    static func pair(for seed: String) -> (Color, Color) {
        var h: UInt64 = 5381
        for b in seed.utf8 { h = (h &* 33) &+ UInt64(b) }
        return pairs[Int(h % UInt64(pairs.count))]
    }
}

struct GeneratedCover: View {
    let title: String
    let seed: String

    var body: some View {
        let (a, b) = Palette.pair(for: seed)
        GeometryReader { geo in
            let w = min(geo.size.width, geo.size.height)
            ZStack(alignment: .bottomLeading) {
                LinearGradient(colors: [b, a], startPoint: .topTrailing, endPoint: .bottomLeading)
                Image("Mark")
                    .resizable().scaledToFit()
                    .foregroundStyle(.white.opacity(w < 90 ? 0.18 : 0.07))
                    .frame(width: w * 0.9, height: w * 0.9)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: w < 90 ? .center : .topTrailing)
                    .offset(x: w < 90 ? 0 : w * 0.22, y: w < 90 ? w * 0.05 : -w * 0.08)
                if !title.isEmpty && w >= 90 { VStack(alignment: .leading, spacing: w * 0.02) {
                    Rectangle().fill(Color.ember).frame(width: w * 0.14, height: max(1.5, w * 0.012))
                    Text(title)
                        .font(.custom(Font.lexendFamily, fixedSize: max(9, w * 0.10)).weight(.medium))
                        .foregroundStyle(.white)
                        .lineLimit(3)
                        .minimumScaleFactor(0.6)
                }
                .padding(w * 0.09) }
            }
            .clipped()
        }
    }
}

/// Circular portrait with initials fallback.
struct Portrait: View {
    let path: String?
    let name: String
    var size: CGFloat = 44

    var body: some View {
        RemoteImage(path: path, balanced: true) {
            ZStack {
                Circle().fill(.quaternary)
                Text(Fmt.initials(name))
                    .font(.custom(Font.lexendFamily, fixedSize: size * 0.34).weight(.regular))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .contentShape(Circle())
    }
}

/// UIKit rendering of the generated cover for the lock screen when no artwork exists.
@MainActor
enum ArtworkRenderer {
    static func placeholder(title: String, seed: String, size: CGFloat = 600, scale: CGFloat = 1) -> UIImage? {
        let r = ImageRenderer(content: GeneratedCover(title: title, seed: seed).frame(width: size, height: size))
        r.scale = scale
        return r.uiImage
    }
}

extension UIImage {
    /// Squares an image by splitting the difference between cropping and stretching: the long side is
    /// cropped to `short × (1 + (r − 1) / 2)` (half the excess), and the remainder is squeezed to a square.
    /// Tall images lose a third of the crop from the top and two thirds from the bottom, keeping faces.
    func balancedSquare(side: CGFloat? = nil) -> UIImage {
        let w = size.width, h = size.height
        guard w > 0, h > 0, abs(w - h) > 1 else { return self }
        let short = min(w, h), long = max(w, h)
        let kept = short * (1 + (long / short - 1) / 2)
        let removed = long - kept
        let crop: CGRect = h > w
            ? CGRect(x: 0, y: removed / 3, width: w, height: kept)
            : CGRect(x: removed / 2, y: 0, width: kept, height: h)
        let out = side ?? min(short * 1.25, 1200)
        let format = UIGraphicsImageRendererFormat.preferred(); format.scale = 1
        return UIGraphicsImageRenderer(size: CGSize(width: out, height: out), format: format).image { _ in
            // Draw the whole image scaled so that `crop` fills the square.
            let sx = out / crop.width, sy = out / crop.height
            draw(in: CGRect(x: -crop.minX * sx, y: -crop.minY * sy, width: w * sx, height: h * sy))
        }
    }
}
