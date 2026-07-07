// build-index.js (в корне репозитория)
const fs = require('fs');
const path = require('path');

const KEY = process.env.XOR_KEY || 'MyM0v1eK3y';

function encrypt(text) {
  let result = '';
  for (let i = 0; i < text.length; i++) {
    result += String.fromCharCode(
      text.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return Buffer.from(result).toString('base64');
}

function decrypt(encoded) {
  const decoded = Buffer.from(encoded, 'base64').toString();
  let result = '';
  for (let i = 0; i < decoded.length; i++) {
    result += String.fromCharCode(
      decoded.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return result;
}

async function buildIndex() {
  // __dirname в корне — это и есть корень репозитория
  const rootDir = __dirname;
  const dataDir = path.join(rootDir, 'data');
  const indexDir = path.join(rootDir, 'index');
  
  console.log('Корень:', rootDir);
  console.log('Data:', dataDir);
  console.log('Index:', indexDir);
  
  // Создаём папку index
  fs.mkdirSync(indexDir, { recursive: true });
  
  // Проверяем data
  if (!fs.existsSync(dataDir)) {
    console.log('❌ data/ не существует');
    fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypt('[]'));
    fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
      lastBuild: new Date().toISOString(),
      totalMovies: 0,
      filesCount: 0
    }));
    return;
  }
  
  const files = fs.readdirSync(dataDir).filter(f => f.endsWith('.bin'));
  console.log(`Найдено ${files.length} .bin файлов`);
  
  if (files.length === 0) {
    console.log('⚠️ Пусто');
    fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypt('[]'));
    fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
      lastBuild: new Date().toISOString(),
      totalMovies: 0,
      filesCount: 0
    }));
    return;
  }
  
  const indexData = [];
  let totalMovies = 0;
  
  for (const file of files) {
    const encrypted = fs.readFileSync(path.join(dataDir, file), 'utf8');
    try {
      const decrypted = decrypt(encrypted);
      const movies = JSON.parse(decrypted);
      
      for (let i = 0; i < movies.length; i += 7) {
        indexData.push(
          movies[i], movies[i + 1], movies[i + 3], movies[i + 5], movies[i + 6]
        );
        totalMovies++;
      }
      console.log(`  ${file}: ${movies.length / 7} фильмов`);
    } catch (err) {
      console.error(`  ❌ ${file}: ${err.message}`);
    }
  }
  
  const encrypted = encrypt(JSON.stringify(indexData));
  fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypted);
  
  fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
    lastBuild: new Date().toISOString(),
    totalMovies,
    filesCount: files.length
  }));
  
  console.log(`✅ Готово: ${totalMovies} фильмов`);
}

buildIndex().catch(err => {
  console.error('💥 Ошибка:', err);
  process.exit(1);
});
