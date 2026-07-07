// build-index.js
const fs = require('fs');
const path = require('path');

const KEY = process.env.XOR_KEY || 'MyM0v1eK3y';

// Только XOR, без base64
function decrypt(data) {
  let result = '';
  for (let i = 0; i < data.length; i++) {
    result += String.fromCharCode(
      data.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return result;
}

function encrypt(text) {
  let result = '';
  for (let i = 0; i < text.length; i++) {
    result += String.fromCharCode(
      text.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
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
    const encrypted = fs.readFileSync(filePath, 'utf8');
    
    try {
      const decrypted = decrypt(encrypted);
      const movies = JSON.parse(decrypted);
      
      // Плоский массив по 7 элементов
      for (let i = 0; i < movies.length; i += 7) {
        indexData.push(
          movies[i],      // id
          movies[i + 1],  // title
          movies[i + 3],  // date
          movies[i + 5],  // genres
          movies[i + 6]   // actresses
        );
        totalMovies++;
      }
      
      console.log(`  ${file}: ${movies.length / 7} фильмов`);
      
    } catch (err) {
      console.error(`  ❌ ${file}: ${err.message}`);
      // Покажи первые 50 символов чтобы понять что там
      console.error(`     Первые 50 байт: ${encrypted.substring(0, 50)}`);
    }
  }
  
  // Сохраняем индекс (тоже чистый XOR, без base64)
  const jsonString = JSON.stringify(indexData);
  const encrypted = encrypt(jsonString);
  
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
