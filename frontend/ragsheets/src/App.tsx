// import { useState } from 'react'
import './App.css'
// import { Button } from './components/ui/button'
import { Card } from './components/ui/card'
import FileUploader from './components/ui/fileuploader'
import PromptInput from './components/ui/promptinput'
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"

function App() {
  // const [count, setCount] = useState(0)

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
          </div>
          <Tabs defaultValue="upload">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="upload">Upload</TabsTrigger>
              <TabsTrigger value="query">Query</TabsTrigger>
            </TabsList>
            <TabsContent value="upload">
              <Card className="p-20">
                <h6 className="pb-6 sm:pb-6">Upload your excel file</h6>
                  <FileUploader />
              </Card>
            </TabsContent>
            <TabsContent value="query">
              <Card className="p-20">
                <h2>Have a question about your document?</h2>
                <PromptInput />
              </Card>
            </TabsContent>
          </Tabs>
          </main>
        </div>
    </div>
  </>
  )
}

export default App
