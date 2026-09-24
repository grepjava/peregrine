//===----------------------------------------------------------------------===//
// Informational (1xx) responses from an ASGI application.
//
//     await send({"type": "http.response.informational", "status": 104,
//                 "headers": [(b"location", b"/uploads/7")]})
//     await send({"type": "http.response.early_hint",
//                 "links": [b"</style.css>; rel=preload; as=style"]})
//
// Any number may go out before `http.response.start`, including while the
// request body is still arriving -- which is the point of a 104, sent before
// the body is read so that a client cut off during it knows where to resume.
// `http.response.early_hint` is the ASGI extension of that name, a 103 whose
// Link fields are the `links` given. `http.response.informational` is
// Peregrine's own, for a 1xx the specification has no message for.
//
// 100 and 101 are the server's to send, for `Expect: 100-continue` and for a
// protocol switch, and an application asking for either is refused. HTTP/1.0
// has no interim responses at all, so there both messages do nothing, as the
// early-hint extension allows.
//===----------------------------------------------------------------------===//

import CPeregrine
import AvianCore
import AvianHTTP
import PeregrinePython

extension Worker {

    /// Sends `http.response.informational`, or `http.response.early_hint` when
    /// `earlyHint` is set. False with a Python exception set when the message
    /// cannot be sent.
    mutating func asgiInformational(_ slot: Int, message: PyObj, earlyHint: Bool) -> Bool {
        let c = table[slot]
        if c.pointee.flags.contains(.responseStarted) {
            pg_err_set_str(pg_exc_runtime(),
                           "an informational response after http.response.start")
            return false
        }

        var status = 103
        if !earlyHint {
            guard let statusObj = pg_dict_get(message, Interned[.status]) else {
                pg_err_set_str(pg_exc_value(), "http.response.informational needs a status")
                return false
            }
            status = Int(pg_int_as_long(statusObj))
            if status < 102 || status > 199 {
                pg_err_set_str(pg_exc_value(),
                               "an informational status is 102 to 199; the server sends 100 and 101")
                return false
            }
        }
        guard var fields = earlyHint ? linkFields(message) : informationalFields(message) else {
            return false
        }
        defer { fields.destroy() }

        if c.pointee.isH3Stream {
            let parent = Int(c.pointee.parentSlot)
            guard parent >= 0, let h3 = table[parent].pointee.h3 else {
                pg_err_set_str(pg_exc_runtime(), "the connection is gone")
                return false
            }
            var block = ByteBuffer()
            defer { block.destroy() }
            h3.encoder.begin(into: &block)
            h3.encoder.encodeStatus(status, into: &block)
            fields.forEach { n, nl, v, vl in
                h3.encoder.encode(name: n, nameLength: nl, value: vl > 0 ? v : emptyH3Byte,
                                  valueLength: vl, into: &block)
            }
            writeH3HeaderBlock(slot, h3, block: &block)
            flushQUIC(parent)
            return true
        }
        if c.pointee.isStream {
            let parent = Int(c.pointee.parentSlot)
            guard parent >= 0, let h2 = table[parent].pointee.h2 else {
                pg_err_set_str(pg_exc_runtime(), "the connection is gone")
                return false
            }
            var block = ByteBuffer()
            defer { block.destroy() }
            h2.encoder.encodeStatus(status, into: &block)
            fields.forEach { n, nl, v, vl in
                h2.encoder.encode(name: n, nameLength: nl, value: vl > 0 ? v : emptyH2Byte,
                                  valueLength: vl, into: &block)
            }
            writeHeaderBlock(slot, h2, block: &block, endStream: false)
            _ = flush(parent)
            return true
        }
        if c.pointee.head.httpMinor == 0 { return true }
        HTTPResponseWriter.writeStatusLine(&c.pointee.write, status: status)
        fields.forEach { n, nl, v, vl in
            // Validated already, so this cannot refuse.
            _ = HTTPResponseWriter.writeHeader(&c.pointee.write, name: ByteSpan(n, nl),
                                               value: ByteSpan(vl > 0 ? v : n, vl))
        }
        HTTPResponseWriter.endHead(&c.pointee.write)
        flushSoon(slot)
        return true
    }

    /// The `headers` of `http.response.informational`, lowercased and checked
    /// against every protocol's rules at once, so the same message is refused
    /// the same way over each of them.
    private func informationalFields(_ message: PyObj) -> InformationalFields? {
        var fields = InformationalFields()
        guard let headerList = pg_dict_get(message, Interned[.headers]),
              pg_is(headerList, Interned.none) == 0 else { return fields }
        guard let materialized = PySeq.iterable(headerList) else {
            pg_err_clear()
            pg_err_set_str(pg_exc_value(),
                           "http.response.informational headers must be an iterable of pairs")
            fields.destroy()
            return nil
        }
        let list = materialized.seq
        defer { if materialized.owned { pg_decref(list) } }
        let count = PySeq.count(list)
        var i = 0
        while i < count {
            guard let item = PySeq.item(list, i), let (nameObj, valueObj) = PySeq.pair(item) else {
                pg_err_set_str(pg_exc_value(),
                               "each informational header must be a (name, value) pair")
                fields.destroy()
                return nil
            }
            i += 1
            guard let nameView = PyBytesView.of(nameObj) else {
                pg_err_set_str(pg_exc_value(), "an informational header name is not bytes")
                fields.destroy()
                return nil
            }
            defer { nameView.release() }
            guard let valueView = PyBytesView.of(valueObj) else {
                pg_err_set_str(pg_exc_value(), "an informational header value is not bytes")
                fields.destroy()
                return nil
            }
            defer { valueView.release() }
            let name = nameView.span
            // A 1xx has no content, and frames nothing, so neither the
            // connection's fields nor a length mean anything on one.
            if name.count == 0 || name.base[0] == 0x3A
                || HTTP2.isConnectionSpecific(name.base, name.count)
                || HTTPResponseWriter.classify(name).contains(.contentLength) {
                pg_err_set_str(pg_exc_value(), "header is not valid on an informational response")
                fields.destroy()
                return nil
            }
            if let failure = fields.append(name: name, value: valueView.span) {
                pg_err_set_str(pg_exc_value(), staticCString(failure))
                fields.destroy()
                return nil
            }
        }
        return fields
    }

    /// The `links` of `http.response.early_hint`, one Link field each.
    private func linkFields(_ message: PyObj) -> InformationalFields? {
        var fields = InformationalFields()
        guard let links = pg_dict_get(message, Interned[.links]),
              pg_is(links, Interned.none) == 0 else { return fields }
        guard let materialized = PySeq.iterable(links) else {
            pg_err_clear()
            pg_err_set_str(pg_exc_value(), "http.response.early_hint links must be an iterable")
            fields.destroy()
            return nil
        }
        let list = materialized.seq
        defer { if materialized.owned { pg_decref(list) } }
        let count = PySeq.count(list)
        var i = 0
        let link: StaticString = "link"
        while i < count {
            guard let item = PySeq.item(list, i), let view = PyBytesView.of(item) else {
                pg_err_set_str(pg_exc_value(), "an early hint link is not bytes")
                fields.destroy()
                return nil
            }
            i += 1
            defer { view.release() }
            if let failure = fields.append(name: ByteSpan(link.utf8Start, link.utf8CodeUnitCount),
                                           value: view.span) {
                pg_err_set_str(pg_exc_value(), staticCString(failure))
                fields.destroy()
                return nil
            }
        }
        return fields
    }
}

/// Header fields copied out of the message, because each protocol encodes
/// them differently and none of them may be half written when one is refused.
struct InformationalFields {
    private var bytes = ByteBuffer()
    /// Name length, then value length, for each field in `bytes` in turn.
    private var lengths: [Int] = []

    /// Appends one field with its name lowercased, or says why it cannot be.
    mutating func append(name: ByteSpan, value: ByteSpan) -> StaticString? {
        if !HTTP2.validFieldValue(value.base, value.count) {
            return "header contains a control character"
        }
        let start = bytes.readableBytes
        bytes.reserve(name.count + value.count)
        var j = 0
        while j < name.count {
            bytes.writeByte(asciiLower(name.base[j]))
            j += 1
        }
        if !HTTP2.validFieldName(UnsafePointer(bytes.pointer(at: bytes.readerOffset + start)),
                                 name.count) {
            return "header name is not a token"
        }
        if value.count > 0 { bytes.write(value.base, value.count) }
        lengths.append(name.count)
        lengths.append(value.count)
        return nil
    }

    func forEach(_ body: (UnsafePointer<UInt8>, Int, UnsafePointer<UInt8>, Int) -> Void) {
        var offset = bytes.readerOffset
        var k = 0
        while k < lengths.count {
            let nl = lengths[k], vl = lengths[k + 1]
            let name = UnsafePointer(bytes.pointer(at: offset))
            body(name, nl, name + nl, vl)
            offset += nl + vl
            k += 2
        }
    }

    mutating func destroy() { bytes.destroy() }
}
