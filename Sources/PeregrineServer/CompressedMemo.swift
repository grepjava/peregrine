//===----------------------------------------------------------------------===//
// --cache-size with --compress: keeping what a cached body compressed to.
//
// A cached copy holds the application's body, not the bytes that went on the
// wire, so one copy serves every protocol and every coding. Without this, each
// hit for a client that accepts a coding compressed the same body again, and
// for a hot compressible response that was most of what a hit cost.
//
// The memo is per worker and holds the result beside the body it was made
// from. A lookup is a hit only when the body the shared cache just handed back
// is byte for byte the one remembered, so nothing about the cache's lifetimes
// -- expiry, invalidation, a newer response taking a copy's place, a copy that
// varies on Accept-Encoding -- has to be mirrored here: a body that changed is
// a miss, and its slot is reused. What it costs is a comparison of the body,
// which is far cheaper than compressing it.
//
// Bounded twice: a fixed number of slots, and a byte budget over everything
// they hold. The least recently used slot goes first.
//===----------------------------------------------------------------------===//

#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif
import CAvian
import AvianCore
import AvianHTTP

struct CompressedMemo {
    struct Slot {
        var hash: UInt64 = 0
        var coding: ContentCoding = .identity
        var lastUse: UInt64 = 0
        var key = ByteBuffer()
        var source = ByteBuffer()
        var encoded = ByteBuffer()

        var bytes: Int { key.allocatedCapacity + source.allocatedCapacity + encoded.allocatedCapacity }
        var inUse: Bool { coding != .identity }

        mutating func clear() {
            hash = 0
            coding = .identity
            lastUse = 0
            key.destroy()
            source.destroy()
            encoded.destroy()
        }
    }

    static let slotCount = 64
    /// A body larger than this is compressed every time rather than kept.
    static let largestSource = 1024 * 1024

    private var slots: UnsafeMutablePointer<Slot>?
    private var budget = 0
    private var used = 0
    private var clock: UInt64 = 0

    init() {}

    /// Sizes the memo for a cache of `cacheMiB`, which is shared by every
    /// worker: an eighth of it, at most 8 MiB, per worker.
    mutating func configure(cacheMiB: Int) {
        guard cacheMiB > 0, slots == nil else { return }
        budget = min(8 * 1024 * 1024, cacheMiB * 1024 * 1024 / 8)
        let p = UnsafeMutablePointer<Slot>.allocate(capacity: CompressedMemo.slotCount)
        p.initialize(repeating: Slot(), count: CompressedMemo.slotCount)
        slots = p
    }

    private static func hash(_ key: ByteSpan, _ coding: ContentCoding) -> UInt64 {
        av_cache_target_hash(key.base, key.count) &+ UInt64(coding.rawValue) &* 0x9E37_79B9_7F4A_7C15
    }

    private static func equal(_ buffer: ByteBuffer, _ span: ByteSpan) -> Bool {
        buffer.readableBytes == span.count
            && (span.count == 0 || memcmp(buffer.readPointer, span.base, span.count) == 0)
    }

    /// The encoding of `source` under `coding` remembered for `key`, if the
    /// body remembered with it is `source` exactly. The span is valid until
    /// the next call that changes the memo.
    mutating func find(key: ByteSpan, coding: ContentCoding, source: ByteSpan) -> ByteSpan? {
        guard let slots else { return nil }
        let h = CompressedMemo.hash(key, coding)
        var i = 0
        while i < CompressedMemo.slotCount {
            let s = slots + i
            i += 1
            guard s.pointee.inUse, s.pointee.hash == h, s.pointee.coding == coding,
                  CompressedMemo.equal(s.pointee.key, key) else { continue }
            guard CompressedMemo.equal(s.pointee.source, source) else { return nil }
            clock &+= 1
            s.pointee.lastUse = clock
            return ByteSpan(UnsafePointer(s.pointee.encoded.readPointer), s.pointee.encoded.readableBytes)
        }
        return nil
    }

    /// Remembers that `source` encodes to `encoded` under `coding` for `key`,
    /// replacing whatever was remembered for the same key and coding.
    mutating func insert(key: ByteSpan, coding: ContentCoding, source: ByteSpan, encoded: ByteSpan) {
        guard let slots, coding != .identity, source.count <= CompressedMemo.largestSource else { return }
        let need = key.count + source.count + encoded.count
        guard need <= budget else { return }
        let h = CompressedMemo.hash(key, coding)

        // The same key and coding, or else a free slot, or else the least
        // recently used one.
        var chosen = -1
        var free = -1
        var oldest = -1
        var i = 0
        while i < CompressedMemo.slotCount {
            let s = slots + i
            if s.pointee.inUse {
                if s.pointee.hash == h && s.pointee.coding == coding
                    && CompressedMemo.equal(s.pointee.key, key) {
                    chosen = i
                    break
                }
                if oldest < 0 || s.pointee.lastUse < slots[oldest].lastUse { oldest = i }
            } else if free < 0 {
                free = i
            }
            i += 1
        }
        if chosen < 0 { chosen = free >= 0 ? free : oldest }
        release(chosen)
        // Room in the budget, oldest first.
        while used + need > budget {
            var victim = -1
            var j = 0
            while j < CompressedMemo.slotCount {
                if slots[j].inUse && (victim < 0 || slots[j].lastUse < slots[victim].lastUse) {
                    victim = j
                }
                j += 1
            }
            if victim < 0 { break }
            release(victim)
        }

        let s = slots + chosen
        s.pointee.hash = h
        s.pointee.coding = coding
        clock &+= 1
        s.pointee.lastUse = clock
        s.pointee.key.write(key.base, key.count)
        if source.count > 0 { s.pointee.source.write(source.base, source.count) }
        if encoded.count > 0 { s.pointee.encoded.write(encoded.base, encoded.count) }
        used += s.pointee.bytes
    }

    private mutating func release(_ index: Int) {
        guard let slots, slots[index].inUse else { return }
        used -= slots[index].bytes
        slots[index].clear()
    }
}
