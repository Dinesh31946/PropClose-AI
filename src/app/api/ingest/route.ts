import { createClient } from '@/lib/supabase';
import { NextResponse } from 'next/server';
import PDFParser from 'pdf2json';
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

export async function POST(req: Request) {
  try {
    const formData = await req.formData();
    const file = formData.get('file') as File;
    const title = formData.get('title') as string;
    const location = formData.get('location') as string;
    const price = formData.get('price') as string;

    if (!file) return NextResponse.json({ error: "No file" }, { status: 400 });

    // 1. Extract Text
    const bytes = await file.arrayBuffer();
    const buffer = Buffer.from(bytes);
    const pdfParser = new (PDFParser as any)(null, 1);
    const extractedText = await new Promise<string>((resolve, reject) => {
      pdfParser.on("pdfParser_dataError", (err: any) => reject(err));
      pdfParser.on("pdfParser_dataReady", () => resolve(pdfParser.getRawTextContent()));
      pdfParser.parseBuffer(buffer);
    });

    // 2. Generate Embedding (The AI Brain part)
    // We use text-embedding-3-small as you requested
    const embeddingResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: extractedText.substring(0, 8000), // OpenAI has a limit, so we take the main content
    });

    const embedding = embeddingResponse.data[0].embedding;

    // 3. Save to Supabase
    const supabase = createClient();
    const { error } = await supabase
      .from('properties')
      .insert([{ 
        name: title, 
        location: location,
        price: price,
        description: extractedText,
        embedding: embedding // This fills the column you saw earlier
      }]);

    if (error) throw error;

    return NextResponse.json({ success: true });
  } catch (error: any) {
    console.error("Ingest Error:", error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}