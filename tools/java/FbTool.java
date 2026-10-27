import kr.dogfoot.hwplib.object.HWPFile;
import kr.dogfoot.hwplib.reader.HWPReader;
import kr.dogfoot.hwp2hwpx.Hwp2Hwpx;
import kr.dogfoot.hwpxlib.object.HWPXFile;
import kr.dogfoot.hwpxlib.reader.HWPXReader;
import kr.dogfoot.hwpxlib.writer.HWPXWriter;
import kr.dogfoot.hwpxlib.tool.textextractor.TextExtractor;
import kr.dogfoot.hwpxlib.tool.textextractor.TextExtractMethod;
import kr.dogfoot.hwpxlib.tool.textextractor.TextMarks;

/** formbench external tool: HWP->HWPX conversion and independent HWPX parse check (hwpxlib). */
public class FbTool {
    public static void main(String[] a) throws Exception {
        if (a.length < 2 && !(a.length == 2)) { System.err.println("usage: FbTool convert in.hwp out.hwpx | check in.hwpx | rewrite in.hwpx out.hwpx | text in.hwpx"); System.exit(2); }
        try {
            if (a[0].equals("convert")) {
                HWPFile f = HWPReader.fromFile(a[1]);
                HWPXFile x = Hwp2Hwpx.toHWPX(f);
                HWPXWriter.toFilepath(x, a[2]);
                System.out.println("{\"status\":\"ok\"}");
            } else if (a[0].equals("blank")) {
                HWPXFile x = kr.dogfoot.hwpxlib.tool.blankfilemaker.BlankFileMaker.make();
                HWPXWriter.toFilepath(x, a[1]);
                System.out.println("{\"status\":\"ok\"}");
            } else if (a[0].equals("check")) {
                HWPXFile x = HWPXReader.fromFilepath(a[1]);
                int secs = x.sectionXMLFileList().count();
                long paras = 0;
                for (int i = 0; i < secs; i++) paras += x.sectionXMLFileList().get(i).countOfPara();
                System.out.println("{\"status\":\"ok\",\"sections\":" + secs + ",\"top_paragraphs\":" + paras + "}");
            } else if (a[0].equals("rewrite")) {
                HWPXFile x = HWPXReader.fromFilepath(a[1]);
                HWPXWriter.toFilepath(x, a[2]);
                System.out.println("{\"status\":\"ok\"}");
            } else if (a[0].equals("text")) {
                HWPXFile x = HWPXReader.fromFilepath(a[1]);
                String t = TextExtractor.extract(x, TextExtractMethod.InsertControlTextBetweenParagraphText, true, new TextMarks());
                System.out.print(t);
            } else { System.err.println("unknown command"); System.exit(2); }
        } catch (Throwable e) {
            String m = String.valueOf(e).replace("\\", "/").replace("\"", "'").replace("\n", " ");
            System.out.println("{\"status\":\"error\",\"error\":\"" + m + "\"}");
            System.exit(1);
        }
    }
}
