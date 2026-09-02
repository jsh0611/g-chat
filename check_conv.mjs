import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';
dotenv.config({ quiet: true });

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY);

const { data, error } = await supabase
  .from('conversations')
  .select('user_id, role, content, created_at')
  .order('created_at', { ascending: false })
  .limit(10);

if (error) { console.error('ERROR', error); process.exit(1); }
console.log(JSON.stringify(data, null, 2));
