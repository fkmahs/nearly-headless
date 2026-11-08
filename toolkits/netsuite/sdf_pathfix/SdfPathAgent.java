import java.lang.constant.ClassDesc;
import java.lang.constant.MethodTypeDesc;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.zip.ZipEntry;

import jdk.internal.classfile.ClassTransform;
import jdk.internal.classfile.Classfile;
import jdk.internal.classfile.CodeTransform;
import jdk.internal.classfile.Opcode;
import jdk.internal.classfile.instruction.InvokeInstruction;

/**
 * Local-path helper for tools/sdf.py. NetSuite File Cabinet folders may contain '|', which Windows cannot
 * store, so this maps '|' to '-' in local paths only:
 * - import: every local path SDF writes, so "/SuiteScripts/Vendor | Module | Part/x.js" lands in
 *   "SuiteScripts/Vendor - Module - Part/x.js";
 * - upload: the local file SDF reads for a File Cabinet path. The target folder sent to NetSuite is
 *   computed separately from the original path and keeps its '|'.
 * Nothing is sent to NetSuite differently.
 */
public final class SdfPathAgent {
    static volatile int rewrites;

    public static String zipName(ZipEntry e) { return local(e.getName()); }

    public static String uploadLocal(String p) { return local(p); }

    public static String resultPath(Object result) {
        try {
            return local((String) result.getClass().getMethod("getPath").invoke(result));
        } catch (ReflectiveOperationException ex) {
            throw new IllegalStateException(ex);
        }
    }

    static String local(String p) {
        if (p == null || p.indexOf('|') < 0) return p;
        rewrites++;
        return p.replace('|', '-');
    }

    /** In uploadFiles, `new File(fileCabinetFolder, path)` gets `uploadLocal(path)`; the NetSuite parent folder
     *  (removeLastSegmentFromPath on the original path) is untouched. */
    static byte[] patchUpload(String name, byte[] bytes, ClassDesc hook) {
        try {
            int[] hits = {0};
            CodeTransform ct = (cb, el) -> {
                if (el instanceof InvokeInstruction ii && ii.opcode() == Opcode.INVOKESPECIAL
                        && ii.owner().asInternalName().equals("java/io/File") && ii.name().stringValue().equals("<init>")
                        && ii.type().stringValue().equals("(Ljava/io/File;Ljava/lang/String;)V")) {
                    hits[0]++;
                    cb.invokestatic(hook, "uploadLocal", MethodTypeDesc.ofDescriptor("(Ljava/lang/String;)Ljava/lang/String;"));
                }
                cb.with(el);
            };
            byte[] out = Classfile.parse(bytes).transform(ClassTransform.transformingMethodBodies(
                    m -> m.methodName().stringValue().equals("uploadFiles"), ct));
            System.err.println("sdf-path-agent: patched " + hits[0] + " call(s) in " + name);
            return out;
        } catch (Throwable t) {
            System.err.println("sdf-path-agent: could not patch " + name + ": " + t);
            return null;
        }
    }

    public static void premain(String args, Instrumentation inst) {
        // The class-file API is JDK-internal in Java 21: open it to this agent only.
        Module base = Object.class.getModule();
        Map<String, Set<Module>> exports = new HashMap<>();
        for (String pkg : new String[] {"jdk.internal.classfile", "jdk.internal.classfile.instruction", "jdk.internal.classfile.constantpool"})
            exports.put(pkg, Set.of(SdfPathAgent.class.getModule()));
        inst.redefineModule(base, Set.of(), exports, Map.of(), Set.of(), Map.of());
        ClassDesc hook = ClassDesc.of("SdfPathAgent");
        inst.addTransformer(new ClassFileTransformer() {
            @Override
            public byte[] transform(ClassLoader loader, String name, Class<?> c, ProtectionDomain pd, byte[] bytes) {
                if ("com/netsuite/ide/core/suiteapp/delegate/UploadFilesToAccountDelegate".equals(name)) {
                    return patchUpload(name, bytes, hook);
                }
                String owner, method, helper, helperDesc;
                if ("com/netsuite/ide/util/FileUtil".equals(name)) {
                    owner = "java/util/zip/ZipEntry"; method = "getName"; helper = "zipName"; helperDesc = "(Ljava/util/zip/ZipEntry;)Ljava/lang/String;";
                } else if ("com/netsuite/ide/core/suiteapp/delegate/importfiles/DownloadFiles".equals(name)) {
                    owner = "com/netsuite/ide/core/suiteapp/delegate/importfiles/Result"; method = "getPath"; helper = "resultPath"; helperDesc = "(Ljava/lang/Object;)Ljava/lang/String;";
                } else {
                    return null;
                }
                try {
                    int[] hits = {0};
                    // Swap `x.getName()` / `x.getPath()` for a static helper with the same stack effect.
                    CodeTransform ct = (cb, el) -> {
                        if (el instanceof InvokeInstruction ii && ii.owner().asInternalName().equals(owner)
                                && ii.name().stringValue().equals(method) && ii.type().stringValue().equals("()Ljava/lang/String;")) {
                            hits[0]++;
                            cb.invokestatic(hook, helper, MethodTypeDesc.ofDescriptor(helperDesc));
                        } else {
                            cb.with(el);
                        }
                    };
                    byte[] out = Classfile.parse(bytes).transform(ClassTransform.transformingMethodBodies(ct));
                    System.err.println("sdf-path-agent: patched " + hits[0] + " call(s) in " + name);
                    return out;
                } catch (Throwable t) {
                    // The JVM silently ignores transformer exceptions; say so, or the import fails later with no clue why.
                    System.err.println("sdf-path-agent: could not patch " + name + ": " + t);
                    return null;
                }
            }
        });
        Runtime.getRuntime().addShutdownHook(new Thread(() -> System.err.println("sdf-path-agent: " + rewrites + " path(s) mapped '|' -> '-'")));
    }
}
