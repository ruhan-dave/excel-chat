import { useState } from 'react'
import './App.css'
import { Button } from './components/ui/button'
import { Card } from './components/ui/card'
import FileUploader from './components/ui/fileuploader'

function App() {
  const [count, setCount] = useState(0)

  return (
    <>
    <div>
      <div className="header p-6 text-xl border-b">RagSheets</div>
    <div className="min-h-screen p-8 pb-8 sm:p-8">      
      <main className="max-w-4xl mx-auto flex flex-col gap-16">
      <div>
      <h1 className="scroll-m-20 text-4xl font-extrabold tracking-tight lg:text-5xl">
        Query your sheets in minutes!
      </h1>
      <p className="leading-7 [&:not(:first-child)]:mt-6">
        Upload a financial datasheet file, and feel free to ask questions about it!
      </p>
      </div>
      <Card className="p-20">
         <h6>Upload your excel</h6>
         <FileUploader />
      </Card>
      </main>

    </div>
    </div>

      
    </>
  )
}

export default App
