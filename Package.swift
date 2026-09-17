// swift-tools-version: 6.1
import PackageDescription

// Peregrine — a Python ASGI/WSGI server written in Swift.
//
// Design constraints baked into this manifest:
//  * No Foundation anywhere. Foundation drags in ARC-heavy bridging types
//    (NSString, NSData, DispatchQueue) that we cannot afford on the hot path.
//  * Every platform syscall lives in aviancore's C target and every CPython
//    macro in CPeregrine, so the Swift side never has to import packed
//    structs, unions or varargs.
//  * Exclusivity checking is off for Peregrine's own targets in release
//    builds. aviancore cannot set unsafe flags, so release builds of the
//    server pass -Xswiftc -enforce-exclusivity=unchecked as well.
//  * Whole-module optimisation + cross-module optimisation so the many tiny
//    `@inlinable` byte-pushing helpers actually collapse.

let sharedSwiftSettings: [SwiftSetting] = [
    .swiftLanguageMode(.v6),
    // Exclusivity checking on a struct-of-pointers connection table costs real
    // time and buys nothing: none of these structs are ever shared.
    .unsafeFlags(["-enforce-exclusivity=unchecked"], .when(configuration: .release)),
    .define("PEREGRINE_RELEASE", .when(configuration: .release)),
]

// PEREGRINE_EXTENSION=1 builds the server as peregrine._native, a CPython
// extension module, instead of as an executable embedding libpython (see
// scripts/build-extension.sh). Python then comes from the interpreter that
// imports the module, so nothing links libpython: `python3.pc` supplies the
// headers only, where `python3-embed.pc` adds -lpython3.x. The two cannot share
// one build, since every target that touches Python would carry the flag.
let hosted = (Context.environment["PEREGRINE_EXTENSION"] ?? "0") != "0"

// PEREGRINE_PYTHON_PC names the pkg-config package exactly. python3.pc and
// python3-embed.pc are only aliases, and an installation is not obliged to
// carry them: a versioned Homebrew keg ships python-3.13.pc alone, and
// pkg-config then answers `python3` with whichever other Python on the search
// path has one. setup.py and scripts/build-extension.sh pass the interpreter's
// versioned name, python-<LDVERSION>, whenever it exists.
let pythonPackage = Context.environment["PEREGRINE_PYTHON_PC"]
    ?? (hosted ? "python3" : "python3-embed")

/// A module of aviancore, the protocol and systems layers Peregrine is built on.
func avian(_ name: String) -> Target.Dependency {
    .product(name: name, package: "aviancore")
}

let package = Package(
    name: "peregrine",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "peregrine", targets: ["peregrine"]),
        .library(name: "Peregrine", targets: ["PeregrineServer"]),
    ],
    dependencies: [
        // The protocol and systems layers: syscalls, TLS, buffers, the poller,
        // HTTP/1.1, HTTP/2, HTTP/3 and QUIC.
        .package(url: "https://github.com/grepjava/aviancore", from: "0.1.1"),
    ],
    targets: [
        // libpython, located through `python3-embed.pc` -- or, for the
        // extension module, only its headers, through `python3.pc`.
        .systemLibrary(
            name: "CPython",
            path: "Sources/CPython",
            pkgConfig: pythonPackage,
            providers: [
                .apt(["python3-dev"]),
                .yum(["python3-devel"]),
                .brew(["python@3.13"]),
            ]
        ),

        // Every CPython construct that is a macro, a union, or variadic.
        .target(
            name: "CPeregrine",
            dependencies: ["CPython"],
            path: "Sources/CPeregrine",
            cSettings: [
                .headerSearchPath("include"),
            ]
        ),

        .target(name: "PeregrinePython",
                dependencies: ["CPython", "CPeregrine", avian("CAvian"), avian("AvianCore")],
                swiftSettings: sharedSwiftSettings),

        .target(name: "PeregrineWSGI",
                dependencies: ["CPeregrine", avian("CAvian"), avian("AvianCore"), avian("AvianHTTP"),
                               "PeregrinePython"],
                swiftSettings: sharedSwiftSettings),

        .target(name: "PeregrineASGI",
                dependencies: ["CPeregrine", avian("CAvian"), avian("AvianCore"), avian("AvianHTTP"),
                               "PeregrinePython"],
                swiftSettings: sharedSwiftSettings),

        .target(name: "PeregrineServer",
                dependencies: ["CPeregrine", avian("CAvian"), avian("AvianCore"), avian("AvianHTTP"),
                               avian("AvianQUIC"), "PeregrinePython", "PeregrineWSGI", "PeregrineASGI"],
                swiftSettings: sharedSwiftSettings),

        .executableTarget(name: "peregrine", dependencies: ["PeregrineServer"],
                          swiftSettings: sharedSwiftSettings),

        // The parsers that read bytes chosen by the peer, with the invariants
        // that have to survive them. A library rather than part of `pgfuzz`
        // because the test suite replays the same corpus through it.
        .target(name: "PeregrineFuzzTargets",
                dependencies: [avian("AvianCore"), avian("AvianHTTP"), avian("AvianQUIC")],
                swiftSettings: sharedSwiftSettings),

        // Not a product: a development tool, built by `swift build` and run by
        // `swift run pgfuzz`, that nobody has to install.
        .executableTarget(name: "pgfuzz",
                          dependencies: [avian("CAvian"), "PeregrineFuzzTargets"],
                          swiftSettings: sharedSwiftSettings),

        .testTarget(name: "PeregrineTests",
                    dependencies: [avian("AvianCore"), avian("AvianHTTP"), avian("AvianQUIC"),
                                   "PeregrineFuzzTargets"],
                    swiftSettings: [.swiftLanguageMode(.v6)]),
    ],
    cLanguageStandard: .gnu11
)

if hosted {
    // A shared object whose Python symbols stay undefined until python loads
    // it. Nothing that has to link on its own -- the executables and the test
    // runner -- can be built from the same flags, so they are left out.
    package.products = [
        .library(name: "PeregrineExtension", type: .dynamic, targets: ["PeregrineExtension"]),
    ]
    package.targets.removeAll { $0.type == .executable || $0.type == .test }
    package.targets.append(
        .target(name: "PeregrineExtension",
                dependencies: ["CPeregrine", avian("CAvian"), "PeregrineServer"],
                swiftSettings: sharedSwiftSettings,
                // Calls from the server into its own functions bind at link
                // time, as they do in the executable, instead of going through
                // the PLT so that another library could interpose them.
                linkerSettings: [
                    .unsafeFlags(["-Xlinker", "-Bsymbolic-functions"], .when(platforms: [.linux])),
                    // Mach-O refuses a library with undefined symbols unless
                    // told that whoever loads it will supply them.
                    .unsafeFlags(["-Xlinker", "-undefined", "-Xlinker", "dynamic_lookup"],
                                 .when(platforms: [.macOS])),
                ]))
}
