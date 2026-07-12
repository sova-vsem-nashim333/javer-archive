// build-index.js
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const msgpack = require('@msgpack/msgpack');

const KEY = process.env.XOR_KEY || 'MyM0v1eK3y';

function decrypt(encryptedPath) {
  const bytes = fs.readFileSync(encryptedPath);
  const keyBytes = Buffer.from(KEY, 'utf8');
  
  const result = Buffer.alloc(bytes.length);
  for (let i = 0; i < bytes.length; i++) {
    result[i] = bytes[i] ^ keyBytes[i % keyBytes.length];
  }
  
  return result.toString('utf8');
}

function encrypt(data) {
  // data может быть строкой или Buffer
  const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data, 'utf8');
  const keyBytes = Buffer.from(KEY, 'utf8');
  
  const result = Buffer.alloc(bytes.length);
  for (let i = 0; i < bytes.length; i++) {
    result[i] = bytes[i] ^ keyBytes[i % keyBytes.length];
  }
  
  return result;
}

/**
 * Нормализация фильма из любого формата
 * Поддерживает новую структуру (c, t, th, ss, mt, rd) и старую (code, title, ...)
 */
function normalizeFilm(film) {
  return {
    code: film.c || film.code || '',
    title: film.t || film.title || '',
    releaseDate: film.rd || film.releaseDate || null,
    genres: (film.mt?.g || film.metadata?.genre || []),
    actresses: (film.mt?.a || film.metadata?.actress || [])
  };
}

async function buildIndex() {
  const rootDir = path.resolve(__dirname, '..');
  const dataDir = path.join(rootDir, 'data');
  const indexDir = path.join(rootDir, 'index');
  
  fs.mkdirSync(indexDir, { recursive: true });
  
  if (!fs.existsSync(dataDir)) {
    console.log('❌ data/ не существует');
    const emptyIndex = msgpack.encode([]);
    const compressed = zlib.gzipSync(emptyIndex);
    fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypt(compressed));
    return;
  }
  
  const files = fs.readdirSync(dataDir).filter(f => f.endsWith('.bin'));
  console.log(`Найдено ${files.length} файлов`);
  
  const indexData = [];
  let totalMovies = 0;
  
  for (const file of files) {
    const filePath = path.join(dataDir, file);
    
    try {
      const decrypted = decrypt(filePath);
      const data = JSON.parse(decrypted);
      
      // Поддержка обоих форматов: films (полный) и f (минифицированный)
      const films = data.films || data.f || [];
      
      for (const film of films) {
        const normalized = normalizeFilm(film);
        
        // Индекс хранит только то, что нужно для поиска
        indexData.push({
          c: normalized.code,        // code
          t: normalized.title,       // title
          rd: normalized.releaseDate, // releaseDate
          g: normalized.genres,      // genres
          a: normalized.actresses    // actresses
        });
        totalMovies++;
      }
      
      console.log(`  ${file}: ${films.length} фильмов`);
      
    } catch (err) {
      console.error(`  ❌ ${file}: ${err.message}`);
    }
  }
  
  // Сохраняем индекс: msgpack → gzip → xor
  const indexBuffer = msgpack.encode(indexData);
  const compressed = zlib.gzipSync(indexBuffer, { level: 9 });
  const encrypted = encrypt(compressed);
  
  fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypted);
  
  // Метаданные индекса
  const sizes = {
    msgpack: indexBuffer.length,
    gzip: compressed.length,
    final: encrypted.length
  };
  
  fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
    lastBuild: new Date().toISOString(),
    totalMovies,
    filesCount: files.length,
    format: 'msgpack+gzip',
    sizes
  }));
  
  console.log(`✅ Готово: ${totalMovies} фильмов в индексе`);
  console.log(`📦 Размеры: msgpack=${sizes.msgpack}B → gzip=${sizes.gzip}B → final=${sizes.final}B`);
  
  // Выводим пример для проверки
  if (indexData.length > 0) {
    console.log('\n📋 Пример записи в индексе:');
    console.log(JSON.stringify(indexData[0], null, 2));
  }
}

buildIndex().catch(err => {
  console.error('💥 Ошибка:', err);
  process.exit(1);
});
