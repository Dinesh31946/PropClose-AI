import { createClient } from '@/lib/supabase';
import { NextResponse } from 'next/server';
import PDFParser from 'pdf2json';
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

// Helper function to split text into chunks
const chunkText = (text: string, size: number) => {
  const chunks = [];
  for (let i = 0; i < text.length; i += size) {
    chunks.push(text.substring(i, i + size));
  }
  return chunks;
};

export async function POST(req: Request) {
  try {
    const formData = await req.formData();
    const file = formData.get('file') as File;
    const title = formData.get('title') as string;
    const location = formData.get('location') as string;
    const price = formData.get('price') as string;

    if (!file) return NextResponse.json({ error: "No file" }, { status: 400 });

    // 1. Extract Text from PDF
    const bytes = await file.arrayBuffer();
    const buffer = Buffer.from(bytes);
    const pdfParser = new (PDFParser as any)(null, 1);
    const extractedText = await new Promise<string>((resolve, reject) => {
      pdfParser.on("pdfParser_dataError", (err: any) => reject(err));
      pdfParser.on("pdfParser_dataReady", () => resolve(pdfParser.getRawTextContent()));
      pdfParser.parseBuffer(buffer);
    });

    const supabase = createClient();

    // 2. Step 1: Save & Activate Project (Smart Summary)
    const normalizePrice = (priceStr: string): number => {
      if (!priceStr) return 0;
      let str = priceStr.toLowerCase().replace(/,/g, '').trim();
      let multiplier = 1;

      if (str.includes('cr') || str.includes('crore')) {
        multiplier = 10000000;
      } else if (str.includes('l') || str.includes('lac') || str.includes('lakh')) {
        multiplier = 100000;
      }

      const numericMatch = str.match(/\d+(\.\d+)?/);
      if (!numericMatch) return 0;

      return Math.round(parseFloat(numericMatch[0]) * multiplier);
    };
    const aiSummary = `Project: ${title}. Location: ${location}. Price starting at: ${price}.`;
    const numericPrice = normalizePrice(price);

    const projectEmbeddingResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: aiSummary,
    });

    const { data: projectData, error: projectError } = await supabase
      .from('properties')
      .insert([{ 
        name: title, 
        location: location,
        price: price,
        price_numeric: numericPrice,
        description: extractedText.substring(0, 2000), // DB ki safety ke liye
        ai_summary: aiSummary,
        embedding: projectEmbeddingResponse.data[0].embedding
      }])
      .select()
      .single();

    if (projectError) throw projectError;

    // 3. Step 3: Deep Knowledge (Brochure Chunking)
    const textChunks = chunkText(extractedText, 1500); // 1500 chars ke tukde
    
    const chunkEmbeddingsResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: textChunks,
    });

    const chunkData = textChunks.map((chunk, index) => ({
      property_id: projectData.id,
      content: chunk,
      embedding: chunkEmbeddingsResponse.data[index].embedding
    }));

    const { error: chunkError } = await supabase
      .from('brochure_chunks')
      .insert(chunkData);

    if (chunkError) throw chunkError;

    return NextResponse.json({ success: true, message: "Project and Brochure AI-Activated!" });
  } catch (error: any) {
    console.error("Ingest Error:", error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
}