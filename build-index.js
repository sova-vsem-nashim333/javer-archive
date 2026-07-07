// build-index.js
const fs = require('fs');
const path = require('path');

const KEY = process.env.XOR_KEY || 'MyM0v1eK3y';

function decrypt(encryptedPath) {
  // Читаем как бинарный буфер, а не как UTF-8 строку
  const bytes = fs.readFileSync(encryptedPath);
  const keyBytes = Buffer.from(KEY, 'utf8');
  
  const result = Buffer.alloc(bytes.length);
  for (let i = 0; i < bytes.length; i++) {
    result[i] = bytes[i] ^ keyBytes[i % keyBytes.length];
  }
  
  return result.toString('utf8');
}

function encrypt(text) {
  const bytes = Buffer.from(text, 'utf8');
  const keyBytes = Buffer.from(KEY, 'utf8');
  
  const result = Buffer.alloc(bytes.length);
  for (let i = 0; i < bytes.length; i++) {
    result[i] = bytes[i] ^ keyBytes[i % keyBytes.length];
  }
  
  return result;
}

async function buildIndex() {
  const rootDir = __dirname;
  const dataDir = path.join(rootDir, 'data');
  const indexDir = path.join(rootDir, 'index');
  
  fs.mkdirSync(indexDir, { recursive: true });
  
  if (!fs.existsSync(dataDir)) {
    console.log('❌ data/ не существует');
    fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypt('[]'));
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
      const films = data.films || [];
      
      for (const film of films) {
        indexData.push({
          t: film.title,
          d: film.releaseDate,
          g: film.metadata?.genre || [],
          a: film.metadata?.actress || []
        });
        totalMovies++;
      }
      
      console.log(`  ${file}: ${films.length} фильмов`);
      
    } catch (err) {
      console.error(`  ❌ ${file}: ${err.message}`);
    }
  }
  
  // Сохраняем индекс (тоже через байты)
  const indexJSON = JSON.stringify(indexData);
  const encrypted = encrypt(indexJSON);
  
  fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypted);
  fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
    lastBuild: new Date().toISOString(),
    totalMovies,
    filesCount: files.length
  }));
  
  console.log(`✅ Готово: ${totalMovies} фильмов в индексе`);
}

buildIndex().catch(err => {
  console.error('💥 Ошибка:', err);
  process.exit(1);
});
