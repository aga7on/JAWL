// Export a compact, deterministic program snapshot for the JAWL Debug Broker.
// @category JAWL

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.mem.MemoryBlock;

import java.io.File;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class JawlExportProgram extends GhidraScript {
    private String quoted(String value) {
        if (value == null) {
            return "null";
        }
        String escaped = value
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t");
        return "\"" + escaped + "\"";
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1) {
            throw new IllegalArgumentException("output JSON path is required");
        }
        int maxFunctions = args.length >= 2 ? Integer.parseInt(args[1]) : 10000;
        File output = new File(args[0]);
        File parent = output.getParentFile();
        if (parent != null) {
            parent.mkdirs();
        }

        try (PrintWriter out = new PrintWriter(output, StandardCharsets.UTF_8)) {
            out.println("{");
            out.println("  \"program\": " + quoted(currentProgram.getName()) + ",");
            out.println("  \"executable_path\": " + quoted(currentProgram.getExecutablePath()) + ",");
            out.println("  \"format\": " + quoted(currentProgram.getExecutableFormat()) + ",");
            out.println("  \"language\": " + quoted(currentProgram.getLanguageID().toString()) + ",");
            out.println("  \"compiler\": " + quoted(currentProgram.getCompilerSpec().getCompilerSpecID().toString()) + ",");
            out.println("  \"image_base\": " + quoted(currentProgram.getImageBase().toString()) + ",");

            out.println("  \"entry_points\": [");
            AddressIterator entries =
                currentProgram.getSymbolTable().getExternalEntryPointIterator();
            boolean first = true;
            while (entries.hasNext()) {
                monitor.checkCancelled();
                Address address = entries.next();
                if (!first) {
                    out.println(",");
                }
                out.print("    " + quoted(address.toString()));
                first = false;
            }
            out.println();
            out.println("  ],");

            out.println("  \"memory_blocks\": [");
            first = true;
            for (MemoryBlock block : currentProgram.getMemory().getBlocks()) {
                monitor.checkCancelled();
                if (!first) {
                    out.println(",");
                }
                out.print(
                    "    {\"name\":" + quoted(block.getName())
                    + ",\"start\":" + quoted(block.getStart().toString())
                    + ",\"end\":" + quoted(block.getEnd().toString())
                    + ",\"size\":" + block.getSize()
                    + ",\"read\":" + block.isRead()
                    + ",\"write\":" + block.isWrite()
                    + ",\"execute\":" + block.isExecute()
                    + "}"
                );
                first = false;
            }
            out.println();
            out.println("  ],");

            out.println("  \"functions\": [");
            FunctionIterator functions =
                currentProgram.getFunctionManager().getFunctions(true);
            first = true;
            int count = 0;
            int externalCount = 0;
            while (functions.hasNext() && count < maxFunctions) {
                monitor.checkCancelled();
                Function function = functions.next();
                if (function.isExternal()) {
                    externalCount++;
                }
                if (!first) {
                    out.println(",");
                }
                out.print(
                    "    {\"name\":" + quoted(function.getName(true))
                    + ",\"entry\":" + quoted(function.getEntryPoint().toString())
                    + ",\"size\":" + function.getBody().getNumAddresses()
                    + ",\"external\":" + function.isExternal()
                    + ",\"thunk\":" + function.isThunk()
                    + "}"
                );
                first = false;
                count++;
            }
            out.println();
            out.println("  ],");
            out.println("  \"function_count_exported\": " + count + ",");
            out.println("  \"external_function_count_exported\": " + externalCount);
            out.println("}");
        }
        println("JAWL_EXPORT=" + output.getAbsolutePath());
    }
}
