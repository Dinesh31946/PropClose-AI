import { createBrowserClient } from '@supabase/ssr'

export const createClient = () => {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !key) {
    // This will print in your VS Code terminal if the keys are missing
    console.error("CRITICAL: Supabase keys are missing in .env.local");
    return null as any;
  }

  return createBrowserClient(url, key);
}