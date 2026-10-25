// Read-only runtime inspection agent. Compile with frida-java-bridge.
// Runtime dumps are private research artifacts, never workflow assets.
import Java from 'frida-java-bridge';

function onJava(callback) {
  return new Promise((resolve, reject) => {
    Java.perform(() => {
      try { resolve(callback()); } catch (error) { reject(String(error)); }
    });
  });
}

rpc.exports = {
  info() {
    return {version: Frida.version, java: Java.available, pid: Process.id};
  },
  classes(pattern) {
    return onJava(() => Java.enumerateLoadedClassesSync().filter(name => new RegExp(pattern, 'i').test(name)));
  },
  inspectclass(name) {
    return onJava(() => {
      const wrapper = Java.use(name);
      const klass = wrapper.class;
      return {
        name: String(klass.getName()),
        methods: Array.from(klass.getDeclaredMethods()).map(method => String(method.toGenericString())),
        fields: Array.from(klass.getDeclaredFields()).map(field => String(field.toGenericString()))
      };
    });
  },
  dexranges() {
    const hits = new Map();
    for (const protection of ['r--', 'rw-', 'r-x']) {
      for (const range of Process.enumerateRanges({protection, coalesce: false})) {
        if (range.size < 112 || range.size > 200 * 1024 * 1024) continue;
        try {
          for (const hit of Memory.scanSync(range.base, range.size, '64 65 78 0a 30 ?? ?? 00')) {
            const base = hit.address;
            const size = base.add(0x20).readU32();
            const header = base.add(0x24).readU32();
            if (size >= 112 && size <= 96 * 1024 * 1024 && header === 112 && base.add(size).compare(range.base.add(range.size)) <= 0) {
              hits.set(String(base), {address: String(base), size, rangeEnd: String(range.base.add(range.size)), source: range.file ? range.file.path : 'anonymous'});
            }
          }
        } catch (_) {}
      }
    }
    const entries = Array.from(hits.values()).sort((a, b) => ptr(a.address).compare(ptr(b.address)));
    for (let i = 0; i < entries.length; i++) {
      const row = entries[i];
      const next = entries.slice(i + 1).find(item => ptr(item.address).compare(ptr(row.rangeEnd)) < 0);
      const end = next ? ptr(next.address) : ptr(row.rangeEnd);
      row.extent = end.sub(ptr(row.address)).toUInt32();
    }
    return entries;
  },
  dumpdex(address, size) {
    if (size < 112 || size > 200 * 1024 * 1024) throw new Error('Invalid DEX size');
    return ptr(address).readByteArray(size);
  }
};
